#!/usr/bin/env python3
"""Daily loss watch: every league bot's marked-to-market equity, once a cadence, into `watch`.

Runs apart from the hourly ingest so a loss or a dead bot shows in minutes. Equity is the bot's
starting capital plus /profit's profit_all_coin, which values open trades at the current rate; its
/balance wallet refreshes at most hourly. A bot is breached once equity falls suggest.toml [watcher] day_loss of
capital below the day's first reading, and stays breached until the next UTC day.

Stage 0 of the money plan: a breach only raises an alert, through the exporter, and no limit stops a
bot. A drill is the exception. It forces one dry-run bot's breach so the loop stops its entries the
way a live breach will:

  python lab/watcher.py --drill <bot>       the next pass stops entries, open trades stay managed
  python lab/watcher.py --drill-end <bot>   clear the breach and start entries again
"""

import argparse
import datetime as dt
import math
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from psycopg.rows import dict_row

from db import connect
from ingest import ROSTER, Fetch, http_fetch
from league import roster_rows
from suggest import load_config

UPSERT = """
INSERT INTO watch (bot, checked_at, seen_at, error, dry_run, state, capital, equity, day, day_open,
                   breached_at, stopped_at)
VALUES (%(bot)s, %(checked_at)s, %(seen_at)s, %(error)s, %(dry_run)s, %(state)s, %(capital)s, %(equity)s,
        %(day)s, %(day_open)s, %(breached_at)s, %(stopped_at)s)
ON CONFLICT (bot) DO UPDATE SET
    checked_at = EXCLUDED.checked_at, seen_at = EXCLUDED.seen_at, error = EXCLUDED.error,
    dry_run = EXCLUDED.dry_run, state = EXCLUDED.state, capital = EXCLUDED.capital,
    equity = EXCLUDED.equity, day = EXCLUDED.day, day_open = EXCLUDED.day_open,
    breached_at = EXCLUDED.breached_at, stopped_at = EXCLUDED.stopped_at
"""
FAILED = """
INSERT INTO watch (bot, checked_at, seen_at, error) VALUES (%(bot)s, %(now)s, %(now)s, %(error)s)
ON CONFLICT (bot) DO UPDATE SET checked_at = EXCLUDED.checked_at, error = EXCLUDED.error
"""


def read(i: int, fetch: Fetch) -> dict:
    cfg, prof, bal = fetch(i, "show_config"), fetch(i, "profit"), fetch(i, "balance")
    capital = bal["starting_capital"]
    equity = capital + prof["profit_all_coin"]
    if not capital > 0 or not math.isfinite(equity):  # an unpriced open trade makes the sum NaN
        raise ValueError(f"no usable equity: capital {capital}, profit {prof['profit_all_coin']}")
    return {"dry_run": cfg.get("dry_run"), "state": cfg.get("state"), "capital": capital, "equity": equity}


def step(prev: dict | None, seen: dict, now: dt.datetime, day_loss: float) -> dict:
    """The row after this reading. The first reading of a UTC day opens it and clears its breach."""
    day = now.date()
    same_day = prev is not None and prev["day"] == day
    day_open = prev["day_open"] if same_day else seen["equity"]
    breached_at = prev["breached_at"] if same_day else None
    drilled = prev is not None and prev["drill_at"] is not None
    if breached_at is None and (drilled or (day_open - seen["equity"]) / seen["capital"] >= day_loss):
        breached_at = now
    return {**seen, "checked_at": now, "seen_at": now, "error": None, "day": day, "day_open": day_open,
            "breached_at": breached_at, "stopped_at": prev["stopped_at"] if same_day or drilled else None}


def tick(conn, bots: list[str], day_loss: float, fetch: Fetch, now: dt.datetime) -> None:
    """One pass. Reads run together, so one hung bot costs one timeout and not a timeout each."""
    def probe(i: int) -> dict | Exception:
        try:
            return read(i, fetch)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, this bot is unseen this pass
            return e

    with conn.cursor(row_factory=dict_row) as cur:
        prev = {r["bot"]: r for r in cur.execute("SELECT * FROM watch").fetchall()}
    with ThreadPoolExecutor(max_workers=len(bots)) as pool:
        seen = list(pool.map(probe, range(len(bots))))
    for i, (bot, s) in enumerate(zip(bots, seen)):
        if isinstance(s, Exception):
            conn.execute(FAILED, {"bot": bot, "now": now, "error": f"{type(s).__name__}: {s}"[:300]})
            continue
        row = step(prev.get(bot), s, now, day_loss)
        # Checked every pass and not only on the breach: a restarted bot comes back running.
        if bot in prev and prev[bot]["drill_at"] is not None and row["dry_run"] is True and row["state"] == "running":
            try:
                fetch(i, "stopentry", True)
                row["stopped_at"] = row["stopped_at"] or now
            except Exception as e:  # noqa: BLE001 - the other bots' rows still count, and the next pass retries
                row["error"] = f"stopentry: {type(e).__name__}: {e}"[:300]
        conn.execute(UPSERT, {"bot": bot, **row})
    conn.commit()


def drill(conn, bot: str) -> None:
    n = conn.execute("UPDATE watch SET drill_at = now() WHERE bot = %s AND dry_run", (bot,)).rowcount
    conn.commit()
    if not n:
        sys.exit(f"watcher: no dry-run bot {bot!r} in watch")
    print(f"watcher: drill set on {bot}; the next pass stops its entries")


def drill_end(conn, bot: str, bots: list[str], fetch: Fetch) -> None:
    """The flag goes first, or the loop would stop the entries this starts again."""
    n = conn.execute("UPDATE watch SET drill_at = NULL, breached_at = NULL, stopped_at = NULL "
                     "WHERE bot = %s AND drill_at IS NOT NULL", (bot,)).rowcount
    conn.commit()
    if not n:
        sys.exit(f"watcher: {bot!r} has no drill running")
    fetch(bots.index(bot), "start", True)
    print(f"watcher: drill over on {bot}; entries started again")


def loop(bots: list[str], cfg: dict) -> None:
    """Sampled, not event driven: a bot publishes no equity event. One pass per cadence."""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print(f"watcher: {len(bots)} bots every {cfg['cadence_seconds']}s, day loss {cfg['day_loss']:.1%}", flush=True)
    while not stop.is_set():
        try:
            with connect() as conn:
                tick(conn, bots, cfg["day_loss"], http_fetch, dt.datetime.now(dt.timezone.utc))
        except Exception as e:  # noqa: BLE001 - a bad pass ages the rows, and the alert on that is the report
            print(f"watcher: pass failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        stop.wait(cfg["cadence_seconds"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drill", metavar="BOT")
    ap.add_argument("--drill-end", metavar="BOT")
    args = ap.parse_args()
    bots = [name for name, _, _ in roster_rows(ROSTER)]
    if args.drill:
        with connect() as conn:
            drill(conn, args.drill)
    elif args.drill_end:
        with connect() as conn:
            drill_end(conn, args.drill_end, bots, http_fetch)
    else:
        loop(bots, load_config()["watcher"])


if __name__ == "__main__":
    main()
