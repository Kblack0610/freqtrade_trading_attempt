#!/usr/bin/env python3
"""Hourly ingest: every league bot's REST API, plus the lab files, into the trader database.

Per bot: closed trades (/trades, paged) and open trades (/status) are upserted by
(bot, trade_id, open_date), and one bot_snapshots row is written from /profit, /balance and
/show_config. Re-running adds no duplicates. An open trade the bot no longer reports (an entry
that timed out unfilled) is deleted. One unreachable bot gets a snapshot with its error and the
others carry on.

From the lab PVC it also imports, idempotently, every runs/<name>/summary.json (keyed by run
name) and league-history.jsonl (keyed by bot and ts), the files the weekly experiments job and
the daily report still write.

Exits 1 if any bot says it is not dry run, so the CronJob fails loudly.
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Callable

from psycopg.types.json import Jsonb

from db import connect
from league import roster_rows

ROSTER = Path(os.environ.get("LEAGUE_ROSTER", "/etc/freqtrade-league/roster.tsv"))
RESULTS = Path(os.environ.get("LAB_RESULTS", "/lab"))
# Pod N of the StatefulSet answers at <set>-N.<service>; both names default to freqtrade-league.
HOST_FMT = os.environ.get("LEAGUE_HOST_FMT", "freqtrade-league-{i}.freqtrade-league:8080")
PAGE = 500  # the most /trades returns per call

Fetch = Callable[[int, str], object]


def http_fetch(i: int, path: str) -> object:
    user = os.environ.get("FT_API_USERNAME", "freqtrader")
    auth = base64.b64encode(f"{user}:{os.environ.get('FT_API_PASSWORD', '')}".encode()).decode()
    req = urllib.request.Request(f"http://{HOST_FMT.format(i=i)}/api/v1/{path}",
                                 headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def ms(value: int | None) -> dt.datetime | None:
    return None if value is None else dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)


def utc(value: str | None) -> dt.datetime | None:
    """Freqtrade's 'YYYY-MM-DD HH:MM:SS' strings are UTC with no offset."""
    if not value:
        return None
    t = dt.datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def trade_row(t: dict) -> dict:
    return {
        "trade_id": t["trade_id"],
        "open_date": ms(t.get("open_timestamp")) or utc(t["open_date"]),
        "pair": t["pair"],
        "is_open": bool(t["is_open"]),
        "close_date": ms(t.get("close_timestamp")) or utc(t.get("close_date")),
        "open_rate": t.get("open_rate"),
        "close_rate": t.get("close_rate"),
        "amount": t.get("amount"),
        "stake_amount": t.get("stake_amount"),
        "profit_ratio": t.get("profit_ratio"),
        "profit_abs": t.get("profit_abs"),
        "exit_reason": t.get("exit_reason"),
        "enter_tag": t.get("enter_tag") or None,
        "raw": Jsonb(t),
    }


def snapshot_row(cfg: dict, prof: dict, bal: dict) -> dict:
    winrate, mdd = prof.get("winrate"), prof.get("max_drawdown")
    return {
        "dry_run": cfg.get("dry_run"),
        "closed_trades": prof.get("closed_trade_count", 0),
        "open_trades": prof.get("trade_count", 0) - prof.get("closed_trade_count", 0),
        "profit_closed_pct": prof.get("profit_closed_percent"),
        "profit_all_pct": prof.get("profit_all_percent"),
        "profit_closed_abs": prof.get("profit_closed_coin"),
        "profit_all_abs": prof.get("profit_all_coin"),
        "winrate_pct": None if winrate is None else 100 * winrate,
        "max_drawdown_pct": None if mdd is None else 100 * mdd,
        "profit_factor": prof.get("profit_factor"),
        "balance_total": bal.get("total"),
        "bot_start": ms(prof.get("bot_start_timestamp")) or utc(prof.get("bot_start_date")),
        "error": None if cfg.get("dry_run") is True else "NOT DRY RUN",
        "raw": Jsonb({"profit": prof, "balance": {k: v for k, v in bal.items() if k != "currencies"}}),
    }


def closed_trades(fetch: Fetch, i: int) -> list[dict]:
    out, offset = [], 0
    while True:
        page = fetch(i, f"trades?limit={PAGE}&offset={offset}&order_by_id=true")
        out += page["trades"]
        offset += len(page["trades"])
        if not page["trades"] or offset >= page["total_trades"]:
            return out


SNAPSHOT_COLS = ["bot", "ts", "source", "dry_run", "closed_trades", "open_trades", "profit_closed_pct",
                 "profit_all_pct", "profit_closed_abs", "profit_all_abs", "winrate_pct", "max_drawdown_pct",
                 "profit_factor", "balance_total", "bot_start", "error", "raw"]
TRADE_COLS = ["bot", "trade_id", "open_date", "pair", "is_open", "close_date", "open_rate", "close_rate", "amount",
              "stake_amount", "profit_ratio", "profit_abs", "exit_reason", "enter_tag", "raw"]


def insert_sql(table: str, cols: list[str], conflict: str) -> str:
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) "
            f"ON CONFLICT {conflict}")


UPSERT_TRADE = insert_sql("bot_trades", TRADE_COLS, "(bot, trade_id, open_date) DO UPDATE SET " + ", ".join(
    f"{c} = EXCLUDED.{c}" for c in TRADE_COLS[3:]) + ", updated_at = now()")
INSERT_SNAPSHOT = insert_sql("bot_snapshots", SNAPSHOT_COLS, "(bot, ts) DO NOTHING")
UPSERT_BOT = """INSERT INTO bots (name, exchange, strategy, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET exchange = EXCLUDED.exchange, strategy = EXCLUDED.strategy,
                last_seen = GREATEST(bots.last_seen, EXCLUDED.last_seen),
                first_seen = LEAST(bots.first_seen, EXCLUDED.first_seen)"""
# History rows are old: they may move first_seen back but never rename a bot's exchange or strategy.
SEEN_BOT = """INSERT INTO bots (name, exchange, strategy, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s)
              ON CONFLICT (name) DO UPDATE SET first_seen = LEAST(bots.first_seen, EXCLUDED.first_seen)"""


def ingest_bot(conn, fetch: Fetch, i: int, name: str, exchange: str, strategy: str, now: dt.datetime) -> dict:
    """One bot in one transaction. Returns the snapshot it wrote."""
    snap: dict = {k: None for k in SNAPSHOT_COLS}
    trades: list[dict] = []
    try:
        cfg, prof, bal = fetch(i, "show_config"), fetch(i, "profit"), fetch(i, "balance")
        snap.update(snapshot_row(cfg, prof, bal))
        trades = [trade_row(t) for t in closed_trades(fetch, i) + list(fetch(i, "status"))]
    except Exception as e:  # one dead bot must not hide the others
        snap["error"] = f"{type(e).__name__}: {e}"
        trades = []
    snap.update(bot=name, ts=now, source="ingest")
    with conn.transaction():
        conn.execute(UPSERT_BOT, (name, exchange, strategy, now, now))
        conn.cursor().executemany(UPSERT_TRADE, [{**t, "bot": name} for t in trades])
        if snap["error"] in (None, "NOT DRY RUN"):  # the fetch completed, so the trade list is whole
            conn.execute("DELETE FROM bot_trades WHERE bot = %s AND is_open AND NOT (trade_id = ANY(%s))",
                         (name, [t["trade_id"] for t in trades]))
        conn.execute(INSERT_SNAPSHOT, snap)
    snap["trade_rows"] = len(trades)
    return snap


def history_row(rec: dict) -> dict:
    """A league-history.jsonl line (written by report.py) as a bot_snapshots row."""
    row = {k: None for k in SNAPSHOT_COLS}
    row.update({k: rec.get(k) for k in ("closed_trades", "open_trades", "profit_closed_pct", "profit_all_pct",
                                         "winrate_pct", "max_drawdown_pct", "profit_factor")})
    row.update(bot=rec["bot"], ts=utc(rec["ts"]), source="report", bot_start=utc(rec.get("bot_start")),
               error=rec.get("error"), raw=Jsonb(rec))
    err = rec.get("error")
    row["dry_run"] = False if err == "NOT DRY RUN" else None if err else True
    return row


def import_history(conn, path: Path) -> int:
    if not path.is_file():
        return 0
    n = 0
    with conn.transaction():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            row = history_row(rec)
            conn.execute(SEEN_BOT, (rec["bot"], rec["exchange"], rec["strategy"], row["ts"], row["ts"]))
            n += conn.execute(INSERT_SNAPSHOT, row).rowcount
    return n


def backtest_row(b: dict) -> dict:
    return {
        "exchange": b["exchange"], "strategy": b["strategy"], "timeframe": b.get("timeframe"),
        "trades": b.get("trades"), "profit_pct": b.get("profit_pct"), "winrate_pct": b.get("winrate_pct"),
        "max_drawdown_pct": b.get("max_drawdown_pct"), "profit_factor": b.get("profit_factor"),
        "sharpe": b.get("sharpe"), "start_date": utc(b.get("start")), "end_date": utc(b.get("end")),
        "lookahead_bias": b.get("lookahead_bias"), "error": b.get("error"),
    }


BACKTEST_COLS = ["run_id", "exchange", "strategy", "timeframe", "trades", "profit_pct", "winrate_pct",
                 "max_drawdown_pct", "profit_factor", "sharpe", "start_date", "end_date", "lookahead_bias", "error"]
INSERT_BACKTEST = insert_sql("backtests", BACKTEST_COLS, "DO NOTHING")


def import_summary(conn, path: Path) -> bool:
    """One experiments run. The runs row and its children are replaced together, so a re-run
    of the same summary is a no-op and a rewritten summary replaces the old rows."""
    s = json.loads(path.read_text())
    name = s.get("run") or path.parent.name
    with conn.transaction():
        old = conn.execute("SELECT raw FROM runs WHERE kind = 'experiments' AND name = %s", (name,)).fetchone()
        if old and old[0] == s:
            return False
        conn.execute("DELETE FROM runs WHERE kind = 'experiments' AND name = %s", (name,))
        run_id = conn.execute("INSERT INTO runs (kind, name, raw) VALUES ('experiments', %s, %s) RETURNING id",
                              (name, Jsonb(s))).fetchone()[0]
        for b in s.get("backtests", []):
            conn.execute(INSERT_BACKTEST, {**backtest_row(b), "run_id": run_id})
        for strategy, v in (s.get("lookahead") or {}).items():
            conn.execute("INSERT INTO bias_checks (run_id, strategy, kind, has_bias, biased_indicators) "
                         "VALUES (%s, %s, 'lookahead', %s, %s)",
                         (run_id, strategy, bool(v.get("has_bias")), v.get("biased_indicators") or None))
        for strategy, res in (s.get("hyperopt") or {}).items():
            conn.execute("INSERT INTO hyperopt_runs (run_id, strategy, result) VALUES (%s, %s, %s)",
                         (run_id, strategy, Jsonb(res)))
    return True


def summaries(results: Path) -> list[Path]:
    return sorted(p for p in results.glob("runs/*/summary.json") if not p.parent.is_symlink())


def main(fetch: Fetch = http_fetch) -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    unsafe = []
    with connect(autocommit=True) as conn:
        for i, (name, exchange, strategy) in enumerate(roster_rows(ROSTER)):
            snap = ingest_bot(conn, fetch, i, name, exchange, strategy, now)
            if snap["error"] == "NOT DRY RUN":
                unsafe.append(name)
            print(f"ingest: {name}: {snap['trade_rows']} trades, error={snap['error']}")
        runs = sum(import_summary(conn, p) for p in summaries(RESULTS))
        hist = import_history(conn, RESULTS / "league-history.jsonl")
        print(f"ingest: {runs} experiments runs imported, {hist} history rows imported")
    if unsafe:
        print(f"ingest: NOT DRY RUN: {unsafe}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
