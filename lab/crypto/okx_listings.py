"""OKX's historical universe: which USDT spot pairs were listed in each month, delisted ones included.

OKX's instruments API lists live pairs only and its candles API refuses a delisted one (code 51001), but the monthly
archive in lab/crypto/archive.py keeps the delisted pair's 1m files if asked for the id (docs tr-449, findings 5-10).
Tardis's free metadata names the ids, ended ones included. A pair's window is its first and last candle in the archive
files, never Tardis's availableTo, which is when Tardis stopped recording and can lag the delisting. A pair counts as
listed in a month when its window overlaps it. `refresh` writes pair_windows and pair_listings; an id with no archive
file (OKX's archive starts 2020-01 and skips some pairs) is counted and returned, never dropped silently. Candles are
read through archive.okx_candles() when a rerun wants a pair's series; none are stored here.
"""

import datetime as dt
import sys

import pandas as pd

from lab.core.api import Api
from lab.crypto import archive
from lab.crypto.listings import months

VENUE = "okx"
QUOTE = "USDT"
# Tardis's free exchange metadata is one call; no limit is published for it (read 2026-11-27, https://docs.tardis.dev/api/exchanges).
TARDIS = Api("tardis_okex", {"base_url": "https://api.tardis.dev/v1", "page_gap_s": 1.0, "retries": 4, "timeout_s": 60})
FROM = pd.Timestamp(archive.OKX_FROM, tz="UTC")
# Tardis's dates are day-grained and its months are UTC while OKX's files start 8 hours early: a month's slack each side.
SLACK = pd.DateOffset(months=1)


def ids() -> list[dict]:
    """Every spot USDT id Tardis has for okex: {id, pair, since, until}, until None for a live one."""
    info = TARDIS.get("/exchanges/okex")
    return [{"id": s["id"], "pair": s["id"].replace("-", "/"), "since": pd.Timestamp(s["availableSince"]),
             "until": pd.Timestamp(s["availableTo"]) if s.get("availableTo") else None}
            for s in info["availableSymbols"] if s["type"] == "spot" and s["id"].endswith(f"-{QUOTE}")]


def span(since: pd.Timestamp, until: pd.Timestamp | None, now: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """The span to ask the archive for, or None when it ended before the archive starts."""
    end = min((until or now) + SLACK, now)
    start = max(since - SLACK, FROM)
    return (start, end) if start < end else None


def refresh(conn, now: pd.Timestamp | None = None) -> tuple[dict[str, tuple[dt.datetime, dt.datetime]], list[str], list[str]]:
    """Rewrite the venue's windows and monthly listings; returns the window of every pair with archive candles, the
    ids with no archive file, and the pairs whose files could not be read, whose old rows stay as they were."""
    now = now or pd.Timestamp.now(tz="UTC")
    found, ended, missing, failed = {}, {}, [], []
    for s in ids():
        sp = span(s["since"], s["until"], now)
        try:
            w = archive.okx_window(s["pair"], *sp) if sp else None
        except Exception as e:  # one id's failure keeps its old rows, not the others'
            failed.append(s["pair"])
            print(f"listings: {VENUE}: {s['id']}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        if w is None:
            missing.append(s["pair"])
            continue
        found[s["pair"]] = (w[0].to_pydatetime(), w[1].to_pydatetime())
        ended[s["pair"]] = s["until"] is not None
    if not found:
        return {}, missing, failed
    with conn.transaction():
        for table in ("pair_windows", "pair_listings"):
            conn.execute(f"DELETE FROM {table} WHERE exchange = %s AND NOT (pair = ANY(%s::text[]))", (VENUE, failed))
        cur = conn.cursor()
        cur.executemany("INSERT INTO pair_windows (exchange, pair, status, first_ts, last_ts) VALUES (%s, %s, %s, %s, %s)",
                        [(VENUE, p, "ENDED" if ended[p] else "LIVE", f, l) for p, (f, l) in found.items()])
        cur.executemany("INSERT INTO pair_listings (exchange, month, pair) VALUES (%s, %s, %s)",
                        [(VENUE, m, p) for p, (f, l) in found.items() for m in months(f, l)])
    print(f"listings: {VENUE}: {len(found)} USDT pairs, {sum(ended.values())} ended, "
          f"{len(missing)} ids with no archive file, {len(failed)} unread", flush=True)
    return found, missing, failed


def main() -> int:
    from lab.core.db import connect
    with connect(autocommit=True) as conn:
        found, missing, failed = refresh(conn)
        return 0 if found and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
