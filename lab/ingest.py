#!/usr/bin/env python3
"""Hourly ingest: every league bot's REST API, plus the lab files, into the trader database.

Per bot: closed trades (/trades, paged) and open trades (/status) are upserted by
(bot, trade_id, open_date), and one bot_snapshots row is written from /profit, /balance and
/show_config, with the newest candle time of each open trade's pair (/pair_candles) for /live.
Re-running adds no duplicates. An open trade the bot no longer reports (an entry that timed out
unfilled) is deleted. One unreachable bot gets a snapshot with its error and the
others carry on.

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

from lab.db import connect
from lab.league import roster_rows
from lab.util import utc

ROSTER = Path(os.environ.get("LEAGUE_ROSTER", "/etc/freqtrade-league/roster.tsv"))
# Pod N of the StatefulSet answers at <set>-N.<service>; both names default to freqtrade-league.
HOST_FMT = os.environ.get("LEAGUE_HOST_FMT", "freqtrade-league-{i}.freqtrade-league:8080")
PAGE = 500  # the most /trades returns per call

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


def ingest_bot(conn, fetch: Fetch, i: int, name: str, exchange: str, strategy: str, now: dt.datetime) -> dict:
    """One bot in one transaction. Returns the snapshot it wrote."""
    snap: dict = {k: None for k in SNAPSHOT_COLS}
    trades: list[dict] = []
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


def main(fetch: Fetch = http_fetch) -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    unsafe = []
    with connect(autocommit=True) as conn:
        for i, (name, exchange, strategy) in enumerate(roster_rows(ROSTER)):
            snap = ingest_bot(conn, fetch, i, name, exchange, strategy, now)
            if snap["error"] == "NOT DRY RUN":
                unsafe.append(name)
            print(f"ingest: {name}: {snap['trade_rows']} trades, error={snap['error']}")
    if unsafe:
        print(f"ingest: NOT DRY RUN: {unsafe}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
