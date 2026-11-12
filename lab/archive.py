"""Exchanges' published candle archives: history their APIs will not serve, or serve too slowly.

Kraken's OHLCVT archive is all the past it has: its API gives only the newest 720 candles.
OKX's monthly 1m files cover what its API pages at 100 candles a call (about 25 hours for our 22 pairs
back to the 1m floor); okx_candles() reads them instead, once per series (lab/backfill.py).

Kraken:

The API gives only the newest 720 candles per timeframe, so anything older comes from these zips.
They are read by HTTP range, one member at a time: the full archive is about 10 GB in five parts and
our pairs are under 1 GB of it, so it is never downloaded or unpacked whole. The parts are plain slices
of one zip. Each member's CRC is checked on read.

Members are `<base><quote>_<minutes>.csv`, no header: unix seconds, open, high, low, close, volume,
trades. Candles with no trades are left out. The archive has no 30m, so that is built from 15m.

A quarterly file covers what the full archive stops short of. Run it once by hand, between backfills:
    python lab/archive.py https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_2026Q4.zip

OKX: one zip per pair and month (months in UTC+8, so each starts 8 hours before the UTC one), a CSV with a
header; the listing call takes at most 10 months per request. The current month is not published yet,
so the API append fills it.

Every request goes through lab/markets/api.py Api, one client per host, paced and retried there. Nothing else reads
these hosts, so none takes turns from a shared budget.
"""

import io
import json
import sys
import zipfile

import pandas as pd

from lab.markets.api import Api

# Static files on Kraken's CDN: no published limit and no Retry-After seen (read 2026-11-11,
# https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data,
# which gives none).
# A range read is a few MB, so a short gap; the timeout covers the biggest member.
KRAKEN = Api("kraken_archive", {"base_url": "https://assets.kraken.com/marketing/institutions", "page_gap_s": 0.2,
                                "retries": 3, "timeout_s": 300})
BASE = f"{KRAKEN.base}/"
FULL = [f"{BASE}Kraken_OHLCVT_Full.zip.part0{i}" for i in range(5)]
ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 15, "1h": 60, "4h": 240, "1d": 1440}
# identity: a Range read must be the file's own bytes, never a re-encoded body.
HEADERS = {"User-Agent": "trader-lab-backfill", "Accept-Encoding": "identity"}
COLS = ["date", "open", "high", "low", "close", "volume"]
# OKX "Get historical market data": "Rate Limit: 5 requests per 2 seconds", "Rate limit rule: IP" (read 2026-11-11,
# https://www.okx.com/docs-v5/en/, Public Data, Get historical market data), so 0.8 s, under 2.5 a second.
OKX = Api("okx", {"base_url": "https://www.okx.com/api/v5/public", "page_gap_s": 0.8, "retries": 4, "timeout_s": 60})
OKX_LIST = f"{OKX.base}/market-data-history"
# The monthly files the listing points at, on OKX's CDN: no published limit (read 2026-11-11, the page above).
OKX_FILES = Api("okx_files", {"base_url": "https://static.okx.com/cdn/okex/traderecords", "page_gap_s": 0.2,
                              "retries": 3, "timeout_s": 300})
OKX_WINDOW = pd.DateOffset(months=9)  # the listing refuses a span over 10 months
OKX_FROM = "20170101"  # before OKX's first spot listing in its files


class RangeFile:
    """Consecutive URLs as one seekable file. zipfile asks for each member in a single read."""

    def __init__(self, urls: list[str], api: Api = KRAKEN):
        self.api, self.parts, self.size, self.pos = api, [], 0, 0
        for url in urls:
            path = path_of(api, url)
            n = int(ok(api.request("HEAD", path, headers=HEADERS)).headers["Content-Length"])
            self.parts.append((self.size, n, path))
            self.size += n

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = (offset, self.pos + offset, self.size + offset)[whence]
        return self.pos

    def read(self, n: int = -1) -> bytes:
        left = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        out = []
        for start, size, path in self.parts:
            if left > 0 and start <= self.pos < start + size:
                a = self.pos - start
                b = min(size, a + left) - 1
                data = ok(self.api.request("GET", path, headers={**HEADERS, "Range": f"bytes={a}-{b}"})).content
                out.append(data)
                self.pos += len(data)
                left -= len(data)
        return b"".join(out)


def path_of(api: Api, url: str) -> str:
    """url's path under api's base; any other host or prefix is refused, so no request leaves the client's base."""
    if not url.startswith(f"{api.base}/"):
        raise ValueError(f"refusing {url}: not under {api.base}/")
    return url[len(api.base):]


def ok(r):
    r.raise_for_status()
    return r


def member(pair: str, timeframe: str) -> str:
    base, quote = pair.split("/")
    return f"{ALIASES.get(base, base)}{quote}_{MINUTES[timeframe]}.csv"


def candles(z: zipfile.ZipFile, pair: str, timeframe: str, floor: str | None = None) -> pd.DataFrame | None:
    """One series as Freqtrade's columns, or None if the archive does not have the pair."""
    name = member(pair, timeframe)
    if name not in z.namelist():
        return None
    df = pd.read_csv(io.BytesIO(z.read(name)), header=None, names=[*COLS, "trades"], usecols=COLS)
    df["date"] = pd.to_datetime(df["date"], unit="s", utc=True)
    if floor:
        df = df[df["date"] >= pd.Timestamp(floor, tz="UTC")]
    if timeframe == "30m":
        df = resample(df, "30m")
    return df.reset_index(drop=True)


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Candles at a longer timeframe, each bucket built from the shorter candles it has; an empty one stays empty."""
    return df.set_index("date").resample(pd.Timedelta(timeframe)).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()


def _get(api: Api, url: str, params: dict | None = None) -> bytes:
    return ok(api.request("GET", path_of(api, url), params, headers=HEADERS)).content


def okx_months(inst_id: str, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    """The URLs of every monthly 1m file OKX publishes for one instrument between start and end."""
    urls: set[str] = set()
    while start < end:
        stop = min(start + OKX_WINDOW, end)
        query = {"module": 2, "instType": "SPOT", "instIdList": inst_id, "dateAggrType": "monthly",
                                        "begin": int(start.timestamp() * 1000), "end": int(stop.timestamp() * 1000)}
        body = json.loads(_get(OKX, OKX_LIST, query))
        if body["code"] != "0":
            raise RuntimeError(f"okx market-data-history {body['code']}: {body['msg']}")
        urls.update(g["url"] for d in body["data"] for det in d["details"] for g in det["groupDetails"])
        start = stop
    return sorted(urls)


def okx_candles(pair: str, floor: str | None = None) -> pd.DataFrame | None:
    """One pair's 1m candles from OKX's monthly files, from floor to the last published month; None if it has none."""
    start = pd.Timestamp(floor or OKX_FROM, tz="UTC")
    frames = []
    for url in okx_months(pair.replace("/", "-"), start, pd.Timestamp.now(tz="UTC")):
        z = zipfile.ZipFile(io.BytesIO(_get(OKX_FILES, url)))
        for name in z.namelist():
            df = pd.read_csv(io.BytesIO(z.read(name)), usecols=["open_time", "open", "high", "low", "close", "vol", "confirm"])
            frames.append(df[df["confirm"] == 1])
    if not frames:
        return None
    df = pd.concat(frames).rename(columns={"open_time": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"], unit="ms", utc=True)
    df = df[df["date"] >= start]
    return df[COLS].sort_values("date").drop_duplicates("date").reset_index(drop=True)


def main() -> int:
    from lab import backfill

    z = zipfile.ZipFile(RangeFile(sys.argv[1:]))
    series = [(p, tf) for p in backfill.pairs_of("kraken") for tf in MINUTES if tf in backfill.TIMEFRAMES]
    done = backfill.import_archive(z, "kraken", series)
    print(f"archive: {len(done)} of {len(series)} series", flush=True)
    return 0 if len(done) == len(series) else 1


if __name__ == "__main__":
    sys.exit(main())
