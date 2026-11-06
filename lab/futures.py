#!/usr/bin/env python3
"""Daily funding rates and open interest for the perpetual swaps of each spot pair we trade.

Through ccxt's public endpoints, into funding_rates and open_interest (migration 018); this is their only writer.
Free history is short and only grows by collecting, so each run fetches from the newest stored row on:
- funding: from FUNDING_FROM on the first run, in 30-day windows (kucoin pages hold 100 rows, 8h funding fills 90);
- open interest: the daily history each exchange serves (okx about 180 days, kucoin about 98), overlapping
  the stored rows, so a missed day is refilled on the next run.
Freqtrade cannot download kucoin futures, so both venues go through ccxt the same way.
Exits 1 if any series failed; the others are still written.
"""

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from db import connect

REPO = Path(os.environ.get("REPO", "/repo"))
VENUES = os.environ.get("FUTURES_EXCHANGES", "okx kucoin").split()
FUNDING_FROM = dt.datetime.fromisoformat(os.environ.get("FUNDING_FROM", "2024-01-01")).replace(tzinfo=dt.timezone.utc)
DAY_MS = 86_400_000
WINDOW_MS = 30 * DAY_MS
CCXT_ID = {"okx": "okx", "kucoin": "kucoinfutures"}
# okx rejects a since on open interest ("Illegal time range") and serves 180 days without one;
# kucoin serves 30 days unless ccxt pages back from a since, then about 98
OI_PAGED = {"okx": False, "kucoin": True}
# okx caps open-interest history at 5 requests per 2s per IP, shared with the league bots, so a 429 waits and retries
RETRY_WAITS = (2.0, 4.0, 8.0)


class Venue(Protocol):
    def swaps(self) -> set[str]: ...
    def funding(self, symbol: str, since: int, until: int) -> list[tuple[int, float]]: ...
    def open_interest(self, symbol: str, since: int | None) -> list[tuple[int, float | None, float | None]]: ...


class Ccxt:
    def __init__(self, exchange: str, sleep=time.sleep):
        import ccxt
        self.ex = getattr(ccxt, CCXT_ID[exchange])({"enableRateLimit": True})
        self.throttled, self.sleep = ccxt.RateLimitExceeded, sleep

    def _call(self, fetch, *args, **kwargs):
        for wait in RETRY_WAITS:
            try:
                return fetch(*args, **kwargs)
            except self.throttled:
                self.sleep(wait)
        return fetch(*args, **kwargs)

    def swaps(self) -> set[str]:
        return {s for s, m in self.ex.load_markets().items() if m.get("swap") and m.get("linear")}

    def funding(self, symbol, since, until):
        rows = self._call(self.ex.fetch_funding_rate_history, symbol, since=since, params={"until": until})
        return [(r["timestamp"], r["fundingRate"]) for r in rows if r.get("fundingRate") is not None]

    def open_interest(self, symbol, since):
        rows = self._call(self.ex.fetch_open_interest_history, symbol, "1d", since=since,
                          params={"paginate": since is not None})
        return [(r["timestamp"], r.get("openInterestAmount"), r.get("openInterestValue")) for r in rows]


def swap_of(pair: str) -> str:
    return f"{pair}:{pair.split('/')[1]}"


def pairs_of(exchange: str) -> list[str]:
    cfg = json.loads((REPO / "user_data" / "exchanges" / f"{exchange}.json").read_text())
    return cfg["exchange"]["pair_whitelist"]


def ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


def utc(m: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(m / 1000, dt.timezone.utc)


def collect_funding(v: Venue, conn, exchange: str, symbol: str, now: int) -> int:
    last = conn.execute("SELECT max(ts) FROM funding_rates WHERE exchange = %s AND pair = %s",
                        (exchange, symbol)).fetchone()[0]
    start, n = (ms(last) + 1 if last else ms(FUNDING_FROM)), 0
    while start < now:
        rows = v.funding(symbol, start, min(start + WINDOW_MS, now))
        with conn.transaction():
            conn.cursor().executemany(
                "INSERT INTO funding_rates (exchange, pair, ts, rate) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [(exchange, symbol, utc(t), r) for t, r in rows])
        n += len(rows)
        # okx ignores until and returns everything after since, so jump past what came back
        start = max([start + WINDOW_MS] + [t + 1 for t, _ in rows])
    return n


def collect_oi(v: Venue, conn, exchange: str, symbol: str, now: int) -> int:
    rows = v.open_interest(symbol, now - 400 * DAY_MS if OI_PAGED.get(exchange) else None)
    with conn.transaction():
        conn.cursor().executemany(
            """INSERT INTO open_interest (exchange, pair, ts, contracts, value_usd) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (exchange, pair, ts) DO UPDATE SET contracts = EXCLUDED.contracts, value_usd = EXCLUDED.value_usd""",
            [(exchange, symbol, utc(t), a, val) for t, a, val in rows])
    return len(rows)


def collect(v: Venue, conn, exchange: str, pairs: list[str], now: int | None = None) -> bool:
    now = now or int(time.time() * 1000)
    listed = v.swaps()
    symbols = [swap_of(p) for p in pairs if swap_of(p) in listed]
    ok, funding, oi = True, 0, 0
    for s in symbols:
        try:
            funding += collect_funding(v, conn, exchange, s, now)
            oi += collect_oi(v, conn, exchange, s, now)
        except Exception as e:  # one delisted or throttled swap must not stop the rest
            print(f"futures: {exchange}: {s}: {type(e).__name__}: {e}", flush=True)
            ok = False
    print(f"futures: {exchange}: {len(symbols)} of {len(pairs)} pairs have a swap, "
          f"{funding} funding and {oi} open interest rows, ok={ok}", flush=True)
    return ok


def main(make=Ccxt) -> int:
    failed = []
    with connect(autocommit=True) as conn:
        for ex in VENUES:
            try:
                ok = collect(make(ex), conn, ex, pairs_of(ex))
            except Exception as e:
                print(f"futures: {ex}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                ok = False
            if not ok:
                failed.append(ex)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
