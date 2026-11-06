"""Prometheus metrics for the league and the lab, read from the trader database at scrape time.

Served on its own port (METRICS_PORT, default 9100) so the public Ingress, which routes only the
site port, never exposes it. home-config scrapes it with a ServiceMonitor and alerts on it.
"""

import datetime as dt
import os

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import GaugeMetricFamily
from psycopg.rows import dict_row

from lab.db import connect

RUN_KINDS = ("analysis", "walkforward")

BOTS = """
SELECT DISTINCT ON (bot) bot, ts, dry_run, error, open_trades, closed_trades, profit_all_pct, balance_total
FROM bot_snapshots ORDER BY bot, ts DESC
"""
WATCH = "SELECT bot, checked_at, seen_at, state, capital, equity, day_open, breached_at FROM watch"
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
FUTURES = """
SELECT 'funding' AS kind, exchange, max(ts) AS newest FROM funding_rates GROUP BY exchange
UNION ALL SELECT 'open_interest', exchange, max(ts) FROM open_interest GROUP BY exchange
"""
SUGGESTIONS = "SELECT kind, count(*) AS n FROM suggestions GROUP BY kind"
# Event-contract markets (lab/markets). A venue counts while it has a recorder sweep in the last week, so a venue
# whose CronJob is removed stops reporting instead of alerting stale forever.
SWEEPS = """
SELECT DISTINCT ON (venue) venue, ok,
       max(finished_at) FILTER (WHERE ok) OVER w AS last_ok,
       count(*) FILTER (WHERE started_at > now() - interval '1 hour') OVER w AS runs_1h,
       count(*) FILTER (WHERE NOT ok AND started_at > now() - interval '1 hour') OVER w AS failed_1h
FROM market_sweeps WHERE started_at > now() - interval '7 days'
WINDOW w AS (PARTITION BY venue)
ORDER BY venue, started_at DESC
"""
LAST_SWEEP = """
SELECT DISTINCT ON (venue) venue, seen, changes, extract(epoch FROM finished_at - started_at) AS took
FROM market_sweeps WHERE ok AND started_at > now() - interval '7 days' ORDER BY venue, started_at DESC
"""
LINKS = "SELECT split_part(source, ':', 1) AS source, status, count(*) AS n FROM market_links GROUP BY 1, 2"
PAPER = "SELECT account, capital, equity, open_positions, marked_at FROM paper_accounts WHERE marked_at IS NOT NULL"


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
                watch = cur.execute(WATCH).fetchall()
                runs = cur.execute(RUNS, (list(RUN_KINDS),)).fetchall()
                candles = cur.execute(CANDLES).fetchall()
                futures = cur.execute(FUTURES).fetchall()
                suggestions = cur.execute(SUGGESTIONS).fetchall()
                sweeps = cur.execute(SWEEPS).fetchall()
                last_sweep = cur.execute(LAST_SWEEP).fetchall()
                links = cur.execute(LINKS).fetchall()
                paper = cur.execute(PAPER).fetchall()
        except Exception:  # noqa: BLE001 - any failure is reported as the db being unreadable
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        yield from bot_metrics(now, bots)
        yield from watch_metrics(now, watch)
        yield from run_metrics(runs)
        yield from candle_metrics(now, candles)
        yield family("trader_futures_age_seconds", "Seconds since the newest funding or open interest row of each exchange",
                     ["kind", "exchange"], (([f["kind"], f["exchange"]], age(now, f["newest"])) for f in futures))
        yield from suggestion_metrics(suggestions)
        yield from market_metrics(now, sweeps, last_sweep, links)
        yield from paper_metrics(now, paper)


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


def watch_metrics(now, watch):
    yield family("trader_watch_age_seconds", "Seconds since the watcher last finished a pass", [],
                 [([], age(now, max((w["checked_at"] for w in watch), default=None)))])
    yield family("trader_watch_unseen_seconds", "Seconds since the watcher last read the bot", ["bot"],
                 (([w["bot"]], age(now, w["seen_at"])) for w in watch))
    yield family("trader_watch_running", "1 when the bot takes entries, 0 when it is paused or stopped", ["bot"],
                 (([w["bot"]], None if w["state"] is None else int(w["state"] == "running")) for w in watch))
    yield family("trader_watch_breached", "1 when the bot broke the daily loss limit this UTC day", ["bot"],
                 (([w["bot"]], int(w["breached_at"] is not None)) for w in watch))
    yield family("trader_watch_day_loss_ratio", "Equity lost since the UTC day's first reading, share of capital",
                 ["bot"], (([w["bot"]], None if w["equity"] is None else (w["day_open"] - w["equity"]) / w["capital"])
                           for w in watch))


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


def market_metrics(now, sweeps, last_sweep, links):
    yield family("trader_markets_sweep_age_seconds", "Seconds since the venue's newest successful recorder sweep ended",
                 ["venue"], (([s["venue"]], age(now, s["last_ok"])) for s in sweeps))
    yield family("trader_markets_sweep_ok", "1 when the venue's newest recorder sweep succeeded", ["venue"],
                 (([s["venue"]], int(s["ok"])) for s in sweeps))
    yield family("trader_markets_sweeps_1h", "Recorder sweeps of the venue started in the last hour", ["venue"],
                 (([s["venue"]], s["runs_1h"]) for s in sweeps))
    yield family("trader_markets_sweeps_failed_1h", "Recorder sweeps of the venue that failed in the last hour, an "
                 "adapter or venue error each", ["venue"], (([s["venue"]], s["failed_1h"]) for s in sweeps))
    yield family("trader_markets_sweep_seen", "Open outcomes in the venue's newest successful sweep", ["venue"],
                 (([s["venue"]], s["seen"]) for s in last_sweep))
    yield family("trader_markets_sweep_changes", "Changed quotes the venue's newest successful sweep wrote", ["venue"],
                 (([s["venue"]], s["changes"]) for s in last_sweep))
    yield family("trader_markets_sweep_duration_seconds", "How long the venue's newest successful sweep took",
                 ["venue"], (([s["venue"]], s["took"]) for s in last_sweep))
    yield family("trader_markets_links", "Cross-venue links by how they were made (rule, llm) and their status",
                 ["source", "status"], (([r["source"], r["status"]], r["n"]) for r in links))


def paper_metrics(now, paper):
    yield family("trader_paper_equity", "A paper account's equity at its last mark: capital, realized, open marked",
                 ["account"], (([p["account"]], p["equity"]) for p in paper))
    yield family("trader_paper_capital", "A paper account's starting capital", ["account"],
                 (([p["account"]], p["capital"]) for p in paper))
    yield family("trader_paper_open_positions", "A paper account's open filled positions", ["account"],
                 (([p["account"]], p["open_positions"]) for p in paper))
    yield family("trader_paper_marked_age_seconds", "Seconds since the paper league last marked the account",
                 ["account"], (([p["account"]], age(now, p["marked_at"])) for p in paper))


def serve(port: int | None = None) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(TraderCollector())
    start_http_server(port or int(os.environ.get("METRICS_PORT", "9100")), registry=registry)
    return registry
