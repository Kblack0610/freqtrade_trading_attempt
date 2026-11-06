"""Kraken's published OHLCVT archive: the history its API will not serve.

The API gives only the newest 720 candles per timeframe, so anything older comes from these zips.
They are read by HTTP range, one member at a time: the full archive is about 10 GB in five parts and
our pairs are under 1 GB of it, so it is never downloaded or unpacked whole. The parts are plain slices
of one zip. Each member's CRC is checked on read.

Members are `<base><quote>_<minutes>.csv`, no header: unix seconds, open, high, low, close, volume,
trades. Candles with no trades are left out. The archive has no 30m, so that is built from 15m.

A quarterly file covers what the full archive stops short of. Run it once by hand, between backfills:
    python lab/archive.py https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_2026Q4.zip
"""

import io
import sys
import urllib.request
import zipfile

import pandas as pd

BASE = "https://assets.kraken.com/marketing/institutions/"
FULL = [f"{BASE}Kraken_OHLCVT_Full.zip.part0{i}" for i in range(5)]
ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 15, "1h": 60, "4h": 240, "1d": 1440}
HEADERS = {"User-Agent": "trader-lab-backfill"}
TIMEOUT = 300
COLS = ["date", "open", "high", "low", "close", "volume"]


class RangeFile:
    """Consecutive URLs as one seekable file. zipfile asks for each member in a single read."""

    def __init__(self, urls: list[str]):
        self.parts, self.size, self.pos = [], 0, 0
        for url in urls:
            head = urllib.request.Request(url, method="HEAD", headers=HEADERS)
            with urllib.request.urlopen(head, timeout=TIMEOUT) as r:
                n = int(r.headers["Content-Length"])
            self.parts.append((self.size, n, url))
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
        for start, size, url in self.parts:
            if left > 0 and start <= self.pos < start + size:
                a = self.pos - start
                b = min(size, a + left) - 1
                req = urllib.request.Request(url, headers={**HEADERS, "Range": f"bytes={a}-{b}"})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read()
                out.append(data)
                self.pos += len(data)
                left -= len(data)
        return b"".join(out)


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
        df = df.set_index("date").resample("30min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()
    return df.reset_index(drop=True)


def main() -> int:
    from lab import backfill

    z = zipfile.ZipFile(RangeFile(sys.argv[1:]))
    series = [(p, tf) for p in backfill.pairs_of("kraken") for tf in MINUTES if tf in backfill.TIMEFRAMES]
    done = backfill.import_archive(z, "kraken", series)
    print(f"archive: {len(done)} of {len(series)} series", flush=True)
    return 0 if len(done) == len(series) else 1


if __name__ == "__main__":
    sys.exit(main())
