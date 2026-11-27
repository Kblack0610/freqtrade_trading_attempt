"""KuCoin's historical universe: which USDT spot pairs were listed in each month, delisted ones included.

KuCoin's API lists live pairs only and refuses a delisted pair's candles (code 400100), but the bucket behind
historical-data.kucoin.com (k-line-history-data) keeps a daily kline zip per symbol and timeframe, delisted symbols
included (docs tr-449, findings 11-13). The bucket is an S3 listing: 1000 entries a page, paged with `marker`, and
`list-type=2` tokens are not returned. A pair's window is its first and last 1h candle in the files; it counts as
listed in a month when its window overlaps it. The archive starts 2022-12-31, so the universe runs from 2023-01 and
cannot test the 2022 bear market. A pair whose last file is within LIVE_DAYS of now is LIVE (files land about a day
late). `refresh` writes pair_windows and pair_listings; `candles` reads a series for a rerun, none are stored here.
Each CSV is `time,open,close,high,low,volume,turnover` with unix-second times in no fixed order: read by column name.
"""

import datetime as dt
import io
import sys
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

from lab.core.api import Api
from lab.crypto.listings import months

VENUE = "kucoin"
QUOTE = "USDT"
TIMEFRAME = "1h"
ROOT = "data/spot/daily/klines/"
FROM = dt.date(2023, 1, 1)
LIVE_DAYS = 3
# No rate limit or terms are published for the bucket (docs tr-449, "Contradicted and unverified"): 5 requests a second.
BUCKET = Api("kucoin_archive", {"base_url": "https://historical-data.kucoin.com", "page_gap_s": 0.2, "retries": 4, "timeout_s": 60})
S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
COLUMNS = ["open", "high", "low", "close", "volume"]


def _list(prefix: str) -> tuple[list[str], list[str]]:
    """Every key and every sub-prefix under a prefix (one level), paged by marker."""
    keys, prefixes, marker = [], [], ""
    while True:
        r = BUCKET.request("GET", "/", {"delimiter": "/", "prefix": prefix, "marker": marker})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        keys += [e.findtext(f"{S3}Key") for e in root.iter(f"{S3}Contents")]
        prefixes += [e.findtext(f"{S3}Prefix") for e in root.iter(f"{S3}CommonPrefixes")]
        if root.findtext(f"{S3}IsTruncated") != "true":
            return keys, prefixes
        nxt = root.findtext(f"{S3}NextMarker") or (keys[-1] if keys else prefixes[-1])
        if nxt == marker:
            raise RuntimeError(f"listing {prefix}: marker {nxt!r} did not advance")
        marker = nxt


def symbols() -> list[str]:
    """Every symbol in the archive quoted in USDT, e.g. 2CRZUSDT."""
    _, prefixes = _list(ROOT)
    return sorted(s for s in (p[len(ROOT):].rstrip("/") for p in prefixes) if s.endswith(QUOTE) and len(s) > len(QUOTE))


def pair_of(symbol: str) -> str:
    return f"{symbol[:-len(QUOTE)]}/{QUOTE}"


def days(symbol: str, tf: str = TIMEFRAME) -> list[dt.date]:
    """The dates that have a file for the series, oldest first (the .CHECKSUM siblings are skipped)."""
    keys, _ = _list(f"{ROOT}{symbol}/{tf}/")
    return sorted(dt.date.fromisoformat(k[-14:-4]) for k in keys if k.endswith(".zip"))


def day_file(symbol: str, tf: str, day: dt.date) -> pd.DataFrame:
    """One day's candles, sorted by time, with a UTC `date` column; empty when the file has no rows."""
    r = BUCKET.request("GET", f"/{ROOT}{symbol}/{tf}/{symbol}-{tf}-{day.isoformat()}.zip")
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    missing = {"time", *COLUMNS} - set(df.columns)
    if missing:
        raise ValueError(f"{symbol} {day}: no column {sorted(missing)} in {list(df.columns)}")
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["date", *COLUMNS]].sort_values("date").reset_index(drop=True)


def candles(symbol: str, tf: str = TIMEFRAME, start: dt.date | None = None, end: dt.date | None = None) -> pd.DataFrame:
    """The series between two dates inclusive, one file a day."""
    ds = [d for d in days(symbol, tf) if (start is None or d >= start) and (end is None or d <= end)]
    frames = [f for f in (day_file(symbol, tf, d) for d in ds) if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", *COLUMNS])


def window(symbol: str) -> tuple[dt.datetime, dt.datetime, dt.date] | None:
    """The first and last 1h candle, and the date of the last file, or None for a symbol with no candle. A file with
    no rows is stepped over from either end."""
    ds = days(symbol)
    first = next((f["date"].iloc[0] for f in (day_file(symbol, TIMEFRAME, d) for d in ds) if not f.empty), None)
    if first is None:
        return None
    last = next(f["date"].iloc[-1] for f in (day_file(symbol, TIMEFRAME, d) for d in reversed(ds)) if not f.empty)
    return first.to_pydatetime(), last.to_pydatetime(), ds[-1]


def refresh(conn, now: pd.Timestamp | None = None) -> tuple[dict[str, tuple[dt.datetime, dt.datetime]], list[str]]:
    """Rewrite the venue's windows and monthly listings; returns the window of every pair with candles, and the pairs
    whose files could not be read, whose old rows stay as they were."""
    now = now or pd.Timestamp.now(tz="UTC")
    found, live, failed = {}, {}, []
    for s in symbols():
        pair = pair_of(s)
        try:
            w = window(s)
        except Exception as e:  # one symbol's failure keeps its old rows, not the others'
            failed.append(pair)
            print(f"listings: {VENUE}: {s}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        if w:
            found[pair], live[pair] = (w[0], w[1]), w[2] >= (now - pd.Timedelta(days=LIVE_DAYS)).date()
    if not found:
        return {}, failed
    floor = FROM.replace(day=1)
    with conn.transaction():
        for table in ("pair_windows", "pair_listings"):
            conn.execute(f"DELETE FROM {table} WHERE exchange = %s AND NOT (pair = ANY(%s::text[]))", (VENUE, failed))
        cur = conn.cursor()
        cur.executemany("INSERT INTO pair_windows (exchange, pair, status, first_ts, last_ts) VALUES (%s, %s, %s, %s, %s)",
                        [(VENUE, p, "LIVE" if live[p] else "ENDED", f, l) for p, (f, l) in found.items()])
        cur.executemany("INSERT INTO pair_listings (exchange, month, pair) VALUES (%s, %s, %s)",
                        [(VENUE, m, p) for p, (f, l) in found.items() for m in months(f, l) if m >= floor])
    print(f"listings: {VENUE}: {len(found)} USDT pairs, {sum(not v for v in live.values())} ended, {len(failed)} unread", flush=True)
    return found, failed


def main() -> int:
    from lab.core.db import connect
    with connect(autocommit=True) as conn:
        found, failed = refresh(conn)
        return 0 if found and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
