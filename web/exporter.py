"""Prometheus metrics for the league and the lab, read from the trader database at scrape time.

Served on its own port (METRICS_PORT, default 9100) so the public Ingress, which routes only the
site port, never exposes it. home-config scrapes it with a ServiceMonitor and alerts on it.
"""

import datetime as dt
import os

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily
from psycopg.rows import dict_row

from db import connect

RUN_KINDS = ("analysis", "walkforward")

BOTS = """
SELECT DISTINCT ON (bot) bot, ts, dry_run, error, open_trades, closed_trades, profit_all_pct, balance_total
FROM bot_snapshots ORDER BY bot, ts DESC
"""
RUNS = """
SELECT k AS kind,
       (SELECT max(started_at) FROM runs WHERE kind = k) AS started_at,
       (SELECT max(finished_at) FROM runs WHERE kind = k) AS finished_at,
       (SELECT ok FROM runs WHERE kind = k AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1) AS ok,
       (SELECT max(finished_at) FROM runs WHERE kind = k AND ok) AS last_ok_at
FROM unnest(%s::text[]) AS k
"""
CANDLES = """
SELECT exchange, max(last_ts) AS newest, max(updated_at) AS updated, count(*) AS series
FROM data_coverage GROUP BY exchange
"""
SUGGESTIONS = "SELECT kind, count(*) AS n FROM suggestions GROUP BY kind"


def age(now: dt.datetime, t: dt.datetime | None) -> float | None:
    return None if t is None else (now - t).total_seconds()


def ts(t: dt.datetime | None) -> float | None:
    return None if t is None else t.timestamp()


class TraderCollector:
    """One read-only pass over the database per scrape; trader_db_up 0 when that pass fails."""

    def __init__(self, conn_factory=connect):
        self.conn_factory = conn_factory

    def describe(self):
        return []

    def collect(self):
        up = GaugeMetricFamily("trader_db_up", "1 when the exporter read the trader database on this scrape")
        try:
            with self.conn_factory() as c, c.cursor(row_factory=dict_row) as cur:
                now = cur.execute("SELECT now() AS now").fetchone()["now"]
                bots = cur.execute(BOTS).fetchall()
                runs = cur.execute(RUNS, (list(RUN_KINDS),)).fetchall()
                candles = cur.execute(CANDLES).fetchall()
                suggestions = cur.execute(SUGGESTIONS).fetchall()
        except Exception:  # noqa: BLE001 - any failure is reported as the db being unreadable
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        yield from bot_metrics(now, bots)
        yield from run_metrics(runs)
        yield from candle_metrics(now, candles)
        yield from suggestion_metrics(suggestions)


def family(name: str, doc: str, labels: list[str], items) -> GaugeMetricFamily:
    g = GaugeMetricFamily(name, doc, labels=labels)
    for values, v in items:
        if v is not None:
            g.add_metric(values, float(v))
    return g


def bot_metrics(now, bots):
    yield family("trader_bot_dry_run", "1 when the bot's newest snapshot reports dry run, 0 when it trades for real",
                 ["bot"], (([b["bot"]], None if b["dry_run"] is None else int(b["dry_run"])) for b in bots))
    yield family("trader_bot_snapshot_age_seconds", "Seconds since the newest ingest snapshot of the bot",
                 ["bot"], (([b["bot"]], age(now, b["ts"])) for b in bots))
    yield family("trader_bot_snapshot_error", "1 when the newest snapshot holds an API error instead of numbers",
                 ["bot"], (([b["bot"]], int(b["error"] is not None)) for b in bots))
    yield family("trader_bot_open_trades", "Open trades in the newest snapshot", ["bot"],
                 (([b["bot"]], b["open_trades"]) for b in bots))
    yield family("trader_bot_closed_trades", "Closed trades in the newest snapshot", ["bot"],
                 (([b["bot"]], b["closed_trades"]) for b in bots))
    yield family("trader_bot_profit_pct", "Profit of all trades, percent of starting balance, newest snapshot",
                 ["bot"], (([b["bot"]], b["profit_all_pct"]) for b in bots))
    newest = max((b["ts"] for b in bots), default=None)
    yield family("trader_ingest_age_seconds", "Seconds since any bot snapshot was written", [],
                 [([], age(now, newest))])


def run_metrics(runs):
    yield family("trader_run_started_timestamp_seconds", "Start of the newest run of each kind", ["kind"],
                 (([r["kind"]], ts(r["started_at"])) for r in runs))
    yield family("trader_run_finished_timestamp_seconds", "End of the newest finished run of each kind", ["kind"],
                 (([r["kind"]], ts(r["finished_at"])) for r in runs))
    yield family("trader_run_ok", "1 when the newest finished run of each kind succeeded", ["kind"],
                 (([r["kind"]], None if r["ok"] is None else int(r["ok"])) for r in runs))
    yield family("trader_run_last_ok_timestamp_seconds", "End of the newest successful run of each kind", ["kind"],
                 (([r["kind"]], ts(r["last_ok_at"])) for r in runs))


def candle_metrics(now, candles):
    yield family("trader_candle_age_seconds", "Seconds since the newest stored candle of each exchange",
                 ["exchange"], (([c["exchange"]], age(now, c["newest"])) for c in candles))
    yield family("trader_coverage_age_seconds", "Seconds since backfill last recorded coverage for each exchange",
                 ["exchange"], (([c["exchange"]], age(now, c["updated"])) for c in candles))
    yield family("trader_coverage_series", "Stored candle series of each exchange", ["exchange"],
                 (([c["exchange"]], c["series"]) for c in candles))


def suggestion_metrics(suggestions):
    yield family("trader_suggestions", "Open suggestions of each kind", ["kind"],
                 (([s["kind"]], s["n"]) for s in suggestions))


def serve(port: int | None = None) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(TraderCollector())
    start_http_server(port or int(os.environ.get("METRICS_PORT", "9100")), registry=registry)
    return registry
