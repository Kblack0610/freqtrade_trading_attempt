"""A venue's historical universe: which USDT pairs were listed in each month, and the candles of the delisted ones.

Binance.US keeps a delisted symbol in api/v3/exchangeInfo as status BREAK, and its klines still serve the delisted
pair's candles (docs tr-449, findings 1-3). So the universe is every TRADING or BREAK symbol quoted in USDT, a pair's
window is its first and last 1h candle (the venue's own record, not a metadata date), and a pair counts as listed in a
month when its window overlaps it. `refresh` writes pair_windows and pair_listings; `backfill_delisted` downloads each
BREAK pair's candles into the backfill datadir, as Freqtrade's download-data would for a live one (it skips a pair the
exchange no longer lists). USDT pairs only: the USD ones were suspended 2023-07-15 to 2025-02-18.
"""

import datetime as dt
import sys

import pandas as pd
from freqtrade.data.converter import ohlcv_to_dataframe
from freqtrade.data.history import get_datahandler
from freqtrade.exchange import timeframe_to_seconds

from lab.core.api import Api
from lab.crypto import backfill

VENUE = "binanceus"
QUOTE = "USDT"
LISTED = {"TRADING", "BREAK"}
# One request every 0.25 s, 4 a second: well under Binance.US's per-IP weight limit (its docs' figure was not re-read
# for this client, so a 429 is simply retried after its Retry-After).
API = Api("binanceus_listings", {"base_url": "https://api.binance.us/api/v3", "page_gap_s": 0.25, "retries": 4, "timeout_s": 30})
PAGE = 1000


def symbols() -> list[dict]:
    """Every TRADING or BREAK symbol quoted in USDT: {symbol, pair, status}."""
    info = API.get("/exchangeInfo")
    return [{"symbol": s["symbol"], "pair": f"{s['baseAsset']}/{QUOTE}", "status": s["status"]}
            for s in info["symbols"] if s["quoteAsset"] == QUOTE and s["status"] in LISTED]


def _kline(symbol: str, **params) -> dt.datetime | None:
    rows = API.get("/klines", {"symbol": symbol, "interval": "1h", "limit": 1, **params})
    return dt.datetime.fromtimestamp(rows[0][0] / 1000, dt.timezone.utc) if rows else None


def window(symbol: str) -> tuple[dt.datetime, dt.datetime] | None:
    """The first and last 1h candle's open time, or None for a symbol with no candles."""
    first = _kline(symbol, startTime=0)
    last = _kline(symbol)
    return (first, last) if first and last else None


def months(first: dt.datetime, last: dt.datetime) -> list[dt.date]:
    """The first day of each calendar month (UTC) from first's to last's."""
    return [d.date() for d in pd.date_range(first.strftime("%Y-%m-01"), last.strftime("%Y-%m-01"), freq="MS")]


def refresh(conn) -> tuple[dict[str, tuple[dt.datetime, dt.datetime]], list[str]]:
    """Rewrite the venue's windows and monthly listings; returns the window of every pair that has candles, and the
    pairs whose candles could not be read, whose old rows stay as they were."""
    found, status, failed = {}, {}, []
    for s in symbols():
        try:
            w = window(s["symbol"])
        except Exception as e:  # one symbol's failure keeps its old rows, not the others'
            failed.append(s["pair"])
            print(f"listings: {VENUE}: {s['symbol']}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        if w:
            found[s["pair"]], status[s["pair"]] = w, s["status"]
    if not found:
        return {}, failed
    with conn.transaction():
        for table in ("pair_windows", "pair_listings"):
            conn.execute(f"DELETE FROM {table} WHERE exchange = %s AND NOT (pair = ANY(%s::text[]))", (VENUE, failed))
        cur = conn.cursor()
        cur.executemany("INSERT INTO pair_windows (exchange, pair, status, first_ts, last_ts) VALUES (%s, %s, %s, %s, %s)",
                        [(VENUE, p, status[p], f, l) for p, (f, l) in found.items()])
        cur.executemany("INSERT INTO pair_listings (exchange, month, pair) VALUES (%s, %s, %s)",
                        [(VENUE, m, p) for p, (f, l) in found.items() for m in months(f, l)])
    print(f"listings: {VENUE}: {len(found)} USDT pairs, {sum(s == 'BREAK' for s in status.values())} delisted, {len(failed)} unread", flush=True)
    return found, failed


def listed_in(conn, month: dt.date) -> list[str]:
    """The pairs the venue listed in the month containing `month`."""
    return [r[0] for r in conn.execute("SELECT pair FROM pair_listings WHERE exchange = %s AND month = %s ORDER BY 1",
                                       (VENUE, month.replace(day=1)))]


def fetch(symbol: str, tf: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """The candles of one series from start to end, paged PAGE a call."""
    step_ms = timeframe_to_seconds(tf) * 1000
    at, stop, rows = int(start.timestamp() * 1000), int(end.timestamp() * 1000), []
    while at <= stop:
        page = API.get("/klines", {"symbol": symbol, "interval": tf, "startTime": at, "endTime": stop, "limit": PAGE})
        rows += [r[:6] for r in page]
        if len(page) < PAGE:
            break
        at = page[-1][0] + step_ms
    return ohlcv_to_dataframe(rows, tf, symbol, fill_missing=False, drop_incomplete=False)


def backfill_delisted(conn, found: dict[str, tuple[dt.datetime, dt.datetime]], timeframes: list[str]) -> bool:
    """Download every delisted pair's candles at each timeframe, and record them in data_coverage (oldest_reached:
    a delisted series has no older candles to find). A series already on disk to its last candle is not fetched again."""
    delisted = {r[0] for r in conn.execute("SELECT pair FROM pair_windows WHERE exchange = %s AND status = 'BREAK'", (VENUE,))}
    served = set(backfill.Cli().timeframes(VENUE))
    dh = get_datahandler(backfill.datadir(VENUE), "feather")
    backfill.datadir(VENUE).mkdir(parents=True, exist_ok=True)
    ok, rows = True, []
    for pair in sorted(delisted & found.keys()):
        first, last = found[pair]
        for tf in (t for t in timeframes if t in served):
            floor = backfill.PREPEND_FLOORS.get(tf)
            start = max(first, pd.Timestamp(floor, tz="UTC").to_pydatetime()) if floor else first
            if start > last:
                continue
            have = backfill.scan(backfill.series_file(VENUE, pair, tf), tf)
            if not have["last_ts"] or have["last_ts"] < last - dt.timedelta(seconds=timeframe_to_seconds(tf)):
                try:
                    new = fetch(pair.replace("/", ""), tf, start, last)
                except Exception as e:
                    print(f"listings: {VENUE}: {pair} {tf}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    ok = False
                    continue
                if not new.empty:
                    backfill.merge_candles(dh, pair, tf, new)
            r = backfill.scan(backfill.series_file(VENUE, pair, tf), tf)
            if r["first_ts"] is None:  # nothing on disk: no coverage row to claim the oldest candle
                continue
            rows.append(r | {"exchange": VENUE, "pair": pair, "timeframe": tf, "oldest_reached": True})
    if rows:
        conn.cursor().executemany(backfill.UPSERT, rows)
    print(f"listings: {VENUE}: {len(rows)} delisted series, ok={ok}", flush=True)
    return ok


def main() -> int:
    from lab.core.db import connect
    with connect(autocommit=True) as conn:
        found, failed = refresh(conn)
        return 0 if found and backfill_delisted(conn, found, backfill.TIMEFRAMES) and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
