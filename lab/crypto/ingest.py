#!/usr/bin/env python3
"""Ingest, run every 5 minutes: every league bot's REST API, plus the lab files, into the trader database.

Per bot: closed trades (/trades, paged) and open trades (/status) are upserted by
(bot, trade_id, open_date), and one bot_snapshots row is written from /profit, /balance and
/show_config, with the newest candle time of each open trade's pair (/pair_candles) for /live.
Each trade the bot recorded its entry candle for (lab/entry_inputs.py, Freqtrade custom data) gets one `crypto`
decision in the same transaction, read from /trades/open/custom-data, and one by one for a trade that closed
within TRACE_CLOSED_HOURS with none yet. A failed custom-data read leaves those trades untraced until the next run.
Re-running adds no duplicates. An open trade the bot no longer reports (an entry that timed out
unfilled) is deleted. One unreachable bot gets a snapshot with its error and the
others carry on. Each run then thins old snapshots, see prune_snapshots.

Exits 1 if any bot says it is not dry run, so the CronJob fails loudly.
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from psycopg.types.json import Jsonb

from lab.core.db import connect
from lab.entry_inputs import KEY as ENTRY_INPUTS
from lab.crypto.league import roster_rows, Row
from lab.core.util import utc

ROSTER = Path(os.environ.get("LEAGUE_ROSTER", "/etc/freqtrade-league/roster.tsv"))
# Pod N of the StatefulSet answers at <set>-N.<service>; both names default to freqtrade-league.
HOST_FMT = os.environ.get("LEAGUE_HOST_FMT", "freqtrade-league-{i}.freqtrade-league:8080")
PAGE = 500  # the most /trades returns per call
# bot_snapshots keeps every row this long, then the last of each hour, then the last of each day for good.
SNAPSHOT_FULL_DAYS = 7
SNAPSHOT_HOURLY_DAYS = 90

LEAGUE = "crypto"  # decisions.league
# A trade that opened and closed between two runs is read one by one, if it closed this recently and has no decision.
TRACE_CLOSED_HOURS = 6

Fetch = Callable[..., object]


def http_fetch(i: int, path: str, post: bool = False) -> object:
    user = os.environ.get("FT_API_USERNAME", "freqtrader")
    auth = base64.b64encode(f"{user}:{os.environ.get('FT_API_PASSWORD', '')}".encode()).decode()
    req = urllib.request.Request(f"http://{HOST_FMT.format(i=i)}/api/v1/{path}",
                                 headers={"Authorization": f"Basic {auth}"}, method="POST" if post else "GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def trade_row(t: dict) -> dict:
    return {
        "trade_id": t["trade_id"],
        "open_date": utc(t.get("open_timestamp")) or utc(t["open_date"]),
        "pair": t["pair"],
        "is_open": bool(t["is_open"]),
        "close_date": utc(t.get("close_timestamp")) or utc(t.get("close_date")),
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


def candles(fetch: Fetch, i: int, timeframe: str | None, pairs: set[str]) -> dict[str, int | None]:
    """Each pair's newest candle open time in epoch ms, as the bot sees it: /live shows its age."""
    if not timeframe:
        return {}
    out = {}
    for pair in sorted(pairs):
        got = fetch(i, f"pair_candles?pair={urllib.parse.quote(pair, safe='')}&timeframe={timeframe}&limit=1")
        out[pair] = got.get("data_stop_ts") or None
    return out


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
        "bot_start": utc(prof.get("bot_start_timestamp")) or utc(prof.get("bot_start_date")),
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


def traced(conn, bot: str, trades: list[dict]) -> set[tuple]:
    """(trade_id, open_date) of the trades that already have their decision."""
    got = conn.execute("SELECT fill_id, decided_at FROM decisions WHERE fill_source = 'freqtrade' AND account = %s "
                       "AND fill_id = ANY(%s)", (bot, [t["trade_id"] for t in trades])).fetchall()
    return {(fid, utc(at)) for fid, at in got}


def entry_inputs(fetch: Fetch, i: int, trades: list[dict], done: set[tuple], now: dt.datetime) -> dict[int, dict]:
    """trade_id -> the entry candle the bot recorded, for each trade still without a decision."""
    def value(entries: object) -> dict[int, dict]:
        return {e["trade_id"]: d["value"] for e in entries for d in e["custom_data"] if d["key"] == ENTRY_INPUTS}

    todo = [t for t in trades if (t["trade_id"], t["open_date"]) not in done]
    got = value(fetch(i, f"trades/open/custom-data?key={ENTRY_INPUTS}&limit=500")) if any(
        t["is_open"] for t in todo) else {}
    since = now - dt.timedelta(hours=TRACE_CLOSED_HOURS)
    for t in todo:
        if not t["is_open"] and t["close_date"] is not None and t["close_date"] >= since:
            got.update(value(fetch(i, f"trades/{t['trade_id']}/custom-data?key={ENTRY_INPUTS}")))
    ids = {t["trade_id"] for t in todo}
    return {tid: v for tid, v in got.items() if tid in ids}


def decision_row(row: Row, t: dict, got: dict) -> dict:
    """A trade's entry as a decision: the strategy's signal, its candle's columns as inputs, the stake as sizing."""
    raw = t["raw"].obj
    tag = t["enter_tag"]
    source = f"{row.exchange} {got['timeframe']} candle"
    return {
        "league": LEAGUE, "account": row.name, "venue": row.exchange, "instrument": t["pair"],
        "decided_at": t["open_date"], "action": "enter",
        "rule": f"{row.strategy}: entry signal" + (f" {tag}" if tag else ""),
        "inputs": Jsonb([{"name": k, "value": v, "source": source, "as_of": got["candle"]}
                         for k, v in got["values"].items()]),
        "checks": Jsonb([]),
        "sizing": Jsonb({"stake": t["stake_amount"], "amount": t["amount"], "price": t["open_rate"],
                         "fee_rate": raw.get("fee_open"), "fee": raw.get("fee_open_cost")}),
        "fill_source": "freqtrade", "fill_id": t["trade_id"],
    }


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
DECISION_COLS = ["league", "account", "venue", "instrument", "decided_at", "action", "rule", "inputs", "checks",
                 "sizing", "fill_source", "fill_id"]
INSERT_DECISION = insert_sql("decisions", DECISION_COLS, "ON CONSTRAINT decisions_fill DO NOTHING")
INSERT_SNAPSHOT = insert_sql("bot_snapshots", SNAPSHOT_COLS, "(bot, ts) DO NOTHING")
UPSERT_BOT = """INSERT INTO bots (name, exchange, strategy, cohort, overlay, first_seen, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET exchange = EXCLUDED.exchange, strategy = EXCLUDED.strategy,
                cohort = EXCLUDED.cohort, overlay = EXCLUDED.overlay,
                last_seen = GREATEST(bots.last_seen, EXCLUDED.last_seen),
                first_seen = LEAST(bots.first_seen, EXCLUDED.first_seen)"""
# A bucket keeps its newest row with a profit figure, since the bot chart skips rows without one. Rows showing a bot
# was not dry run are the record of a safety breach: they are never candidates, so they stay.
PRUNE_SNAPSHOTS = """
DELETE FROM bot_snapshots WHERE (bot, ts) IN (
    SELECT bot, ts FROM (
        SELECT bot, ts, row_number() OVER (
            PARTITION BY bot, date_trunc(CASE WHEN ts < %(daily_before)s THEN 'day' ELSE 'hour' END, ts, 'UTC')
            ORDER BY profit_all_pct IS NULL, ts DESC) AS n
        FROM bot_snapshots
        WHERE ts < %(hourly_before)s AND dry_run IS DISTINCT FROM false AND error IS DISTINCT FROM 'NOT DRY RUN'
    ) ranked WHERE n > 1)
"""


def prune_snapshots(conn, now: dt.datetime) -> int:
    """Thin old snapshots to their tier. The cutoffs sit on UTC bucket starts, so no bucket is thinned half way."""
    hourly_before = (now - dt.timedelta(days=SNAPSHOT_FULL_DAYS)).replace(minute=0, second=0, microsecond=0)
    daily_before = (now - dt.timedelta(days=SNAPSHOT_HOURLY_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(PRUNE_SNAPSHOTS, {"hourly_before": hourly_before, "daily_before": daily_before}).rowcount


def ingest_bot(conn, fetch: Fetch, i: int, row: Row, now: dt.datetime) -> dict:
    """Roster row i's bot in one transaction. Returns the snapshot it wrote."""
    name = row.name
    snap: dict = {k: None for k in SNAPSHOT_COLS}
    trades: list[dict] = []
    decisions: list[dict] = []
    try:
        cfg, prof, bal = fetch(i, "show_config"), fetch(i, "profit"), fetch(i, "balance")
        snap.update(snapshot_row(cfg, prof, bal))
        status = list(fetch(i, "status"))
        trades = [trade_row(t) for t in closed_trades(fetch, i) + status]
        snap["raw"].obj.update(timeframe=cfg.get("timeframe"),
                               candles=candles(fetch, i, cfg.get("timeframe"), {t["pair"] for t in status}))
    except Exception as e:  # one dead bot must not hide the others
        snap["error"] = f"{type(e).__name__}: {e}"
        trades = []
    if trades:
        try:
            got = entry_inputs(fetch, i, trades, traced(conn, name, trades), now)
            decisions = [decision_row(row, t, got[t["trade_id"]]) for t in trades if t["trade_id"] in got]
        except Exception as e:  # the trades still land; their decisions follow on a later run
            snap["raw"].obj["trace_error"] = f"{type(e).__name__}: {e}"
    snap.update(bot=name, ts=now, source="ingest")
    with conn.transaction():
        conn.execute(UPSERT_BOT, (*row, now, now))
        conn.cursor().executemany(UPSERT_TRADE, [{**t, "bot": name} for t in trades])
        conn.cursor().executemany(INSERT_DECISION, decisions)
        if snap["error"] in (None, "NOT DRY RUN"):  # the fetch completed, so the trade list is whole
            conn.execute("DELETE FROM bot_trades WHERE bot = %s AND is_open AND NOT (trade_id = ANY(%s))",
                         (name, [t["trade_id"] for t in trades]))
        conn.execute(INSERT_SNAPSHOT, snap)
    snap["trade_rows"], snap["decision_rows"] = len(trades), len(decisions)
    return snap


def main(fetch: Fetch = http_fetch) -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    unsafe = []
    with connect(autocommit=True) as conn:
        for i, row in enumerate(roster_rows(ROSTER)):
            snap = ingest_bot(conn, fetch, i, row, now)
            if snap["error"] == "NOT DRY RUN":
                unsafe.append(row.name)
            print(f"ingest: {row.name}: {snap['trade_rows']} trades, {snap['decision_rows']} traced, error={snap['error']}")
        print(f"ingest: pruned {prune_snapshots(conn, now)} snapshots")
    if unsafe:
        print(f"ingest: NOT DRY RUN: {unsafe}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
