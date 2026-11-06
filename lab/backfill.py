#!/usr/bin/env python3
"""Hourly price-data backfill into LAB_RESULTS/data/<exchange>, with its coverage in data_coverage.

Per exchange and timeframe, using Freqtrade's own download-data:
- a series that has not reached the exchange's oldest candle gets a prepend pass from PREPEND_FROM,
  or from its timeframe's PREPEND_FLOORS date (1m from 2024: a full 1m history is about 30x the 30m
  one for a few scalping strategies); one call reaches the listing date or the floor;
- then every series gets an append pass up to now. Prepend drops a series' newest candle, so the
  append always runs after it.
A series is marked oldest_reached when a prepend pass leaves its first candle where it was (the
listing date, or the floor), and is never prepended again.

An exchange with no OHLCV history (FORWARD_EXCHANGES, i.e. Kraken, which serves only its newest
720 candles) takes its past from the exchange's published archive (lab/archive.py), once per series,
and is collected forward instead: each run fetches those candles through ccxt and merges
them into the same Freqtrade files. A run at least every 12 hours keeps 1m gap-free (60 hours for 5m). These are the
candles the live bots trade on, and nothing else has them until the next quarterly archive, so the files are kept
forever. Raw trades (Freqtrade's --dl-trades) do not scale here: Kraken BTC/USD prints about 300k
trades a day, and every run reloads and re-converts the whole trades file.

Exchanges run in parallel, since each has its own rate limit. Exits 1 if any download call
failed; coverage is still written for whatever is on disk.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import ccxt
import pandas as pd
from freqtrade.data.converter import clean_ohlcv_dataframe, ohlcv_to_dataframe
from freqtrade.data.history import get_datahandler
from freqtrade.enums import CandleType
from freqtrade.exchange import timeframe_to_seconds
from freqtrade.misc import pair_to_filename

from lab import archive
from lab.db import connect

REPO = Path(os.environ.get("REPO", "/repo"))
RESULTS = Path(os.environ.get("LAB_RESULTS", "/lab"))
EXCHANGES = os.environ.get("BACKFILL_EXCHANGES", "binanceus coinbase kraken").split()
TIMEFRAMES = os.environ.get("TIMEFRAMES", "1m 5m 15m 30m 1h 4h 1d").split()
FORWARD_EXCHANGES = set(os.environ.get("FORWARD_EXCHANGES", "kraken").split())
PREPEND_FROM = os.environ.get("PREPEND_FROM", "20150101")
PREPEND_FLOORS = dict(f.split("=", 1) for f in os.environ.get("PREPEND_FLOORS", "1m=20240101").split())
# Freqtrade lines worth keeping in the job log; the rest is progress bars and config chatter.
KEEP = re.compile(r"Downloaded data for| - (ERROR|WARNING) - ")
SKIP = re.compile(r"not officially supported")


class Freqtrade(Protocol):
    def download(self, exchange: str, args: list[str]) -> bool: ...
    def timeframes(self, exchange: str) -> list[str]: ...
    def recent(self, exchange: str, pair: str, timeframe: str) -> list[list]: ...
    def archive(self, exchange: str) -> zipfile.ZipFile | None: ...


def datadir(exchange: str) -> Path:
    return RESULTS / "data" / exchange


class Cli:
    """The real thing: one freqtrade process per download, ccxt for the newest candles."""

    def __init__(self):
        self._ccxt: dict[str, ccxt.Exchange] = {}  # one per exchange, each used only by its own thread

    def _base(self, exchange: str) -> list[str]:
        ud = REPO / "user_data"
        return ["--userdir", str(ud), "-c", str(ud / "config.base.json"), "-c", str(ud / "exchanges" / f"{exchange}.json")]

    def download(self, exchange: str, args: list[str]) -> bool:
        cmd = ["freqtrade", "download-data", *self._base(exchange), "--datadir", str(datadir(exchange)), *args]
        p = subprocess.run(cmd, capture_output=True, text=True)
        lines = (p.stdout + p.stderr).splitlines()
        errors = [line for line in lines if " - ERROR - " in line]
        for line in lines:
            if KEEP.search(line) and not SKIP.search(line):
                print(f"backfill: {exchange}: {line.split(' - ', 3)[-1]}", flush=True)
        if p.returncode:
            print(f"backfill: {exchange}: download-data exit {p.returncode}: " + " | ".join(lines[-3:]), flush=True)
        return p.returncode == 0 and not errors

    def _client(self, exchange: str) -> ccxt.Exchange:
        if exchange not in self._ccxt:
            self._ccxt[exchange] = getattr(ccxt, exchange)({"enableRateLimit": True})
        return self._ccxt[exchange]

    def timeframes(self, exchange: str) -> list[str]:
        """ccxt's static list, the one freqtrade list-timeframes prints, with no network call."""
        return list(self._client(exchange).timeframes or {})

    def recent(self, exchange: str, pair: str, timeframe: str) -> list[list]:
        """The newest candles the exchange serves, as ccxt [ms, open, high, low, close, volume] rows."""
        return self._client(exchange).fetch_ohlcv(pair, timeframe)

    def archive(self, exchange: str) -> zipfile.ZipFile | None:
        return zipfile.ZipFile(archive.RangeFile(archive.FULL)) if exchange == "kraken" else None


def pairs_of(exchange: str) -> list[str]:
    cfg = json.loads((REPO / "user_data" / "exchanges" / f"{exchange}.json").read_text())
    return cfg["exchange"]["pair_whitelist"]


def series_file(exchange: str, pair: str, timeframe: str) -> Path:
    return datadir(exchange) / f"{pair_to_filename(pair)}-{timeframe}.feather"


def scan(path: Path, timeframe: str) -> dict:
    """First and last candle, row count, and the gaps between them."""
    out = {"first_ts": None, "last_ts": None, "rows": 0, "gaps": 0, "missing_candles": 0}
    if not path.exists():
        return out
    ts = pd.read_feather(path, columns=["date"])["date"]
    if ts.empty:
        return out
    ts = ts.sort_values()
    step = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))
    over = ts.diff().dropna()
    over = over[over > step]
    out.update(first_ts=ts.iloc[0].to_pydatetime(), last_ts=ts.iloc[-1].to_pydatetime(), rows=len(ts),
               gaps=len(over), missing_candles=int(round(sum(o / step - 1 for o in over))))
    return out


def merge_candles(dh, pair: str, tf: str, new: pd.DataFrame) -> None:
    old = dh.ohlcv_load(pair, tf, CandleType.SPOT, fill_missing=False, warn_no_data=False)
    merged = new if old.empty else pd.concat([old, new], ignore_index=True)
    dh.ohlcv_store(pair, tf, clean_ohlcv_dataframe(merged, tf, pair, fill_missing=False, drop_incomplete=False),
                   CandleType.SPOT)


def collect_forward(ft: Freqtrade, exchange: str, pairs: list[str], timeframes: list[str]) -> bool:
    datadir(exchange).mkdir(parents=True, exist_ok=True)
    dh = get_datahandler(datadir(exchange), "feather")
    ok = True
    for pair in pairs:
        for tf in timeframes:
            try:
                # the newest candle is still forming, so it is dropped until the next run
                new = ohlcv_to_dataframe(ft.recent(exchange, pair, tf), tf, pair, fill_missing=False, drop_incomplete=True)
            except Exception as e:
                print(f"backfill: {exchange}: {pair} {tf}: {type(e).__name__}: {e}", flush=True)
                ok = False
                continue
            merge_candles(dh, pair, tf, new)
    return ok


def import_archive(z: zipfile.ZipFile, exchange: str, series: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Merge the archive into these series; returns the ones it finished. A pair the archive lacks has nothing older."""
    datadir(exchange).mkdir(parents=True, exist_ok=True)
    dh = get_datahandler(datadir(exchange), "feather")
    done = set()
    for pair, tf in series:
        try:
            new = archive.candles(z, pair, tf, PREPEND_FLOORS.get(tf))
            if new is not None and not new.empty:
                merge_candles(dh, pair, tf, new)
            done.add((pair, tf))
        except Exception as e:
            print(f"backfill: {exchange}: archive {pair} {tf}: {type(e).__name__}: {e}", flush=True)
    return done


def reached(prior: dict | None, first_ts: dt.datetime | None, prepended: bool) -> bool:
    if prior and prior["oldest_reached"]:
        return True
    return prepended and prior is not None and prior["first_ts"] is not None and first_ts == prior["first_ts"]


UPSERT = """
INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, gaps, missing_candles,
                           oldest_reached, updated_at)
VALUES (%(exchange)s, %(pair)s, %(timeframe)s, %(first_ts)s, %(last_ts)s, %(rows)s, %(gaps)s,
        %(missing_candles)s, %(oldest_reached)s, now())
ON CONFLICT (exchange, pair, timeframe) DO UPDATE SET
    first_ts = EXCLUDED.first_ts, last_ts = EXCLUDED.last_ts, rows = EXCLUDED.rows, gaps = EXCLUDED.gaps,
    missing_candles = EXCLUDED.missing_candles, oldest_reached = EXCLUDED.oldest_reached, updated_at = now()
"""


def backfill_exchange(ft: Freqtrade, conn, exchange: str, pairs: list[str], timeframes: list[str]) -> bool:
    prior = {(p, tf): {"first_ts": f, "oldest_reached": r} for p, tf, f, r in conn.execute(
        "SELECT pair, timeframe, first_ts, oldest_reached FROM data_coverage WHERE exchange = %s", (exchange,))}
    ok = True
    prepended: set[tuple[str, str]] = set()
    supported = set(ft.timeframes(exchange))
    tfs = [tf for tf in timeframes if tf in supported]
    if skipped := [tf for tf in timeframes if tf not in supported]:
        print(f"backfill: {exchange}: no {' '.join(skipped)} candles on this exchange, skipped", flush=True)
    archived: set[tuple[str, str]] = set()
    if exchange in FORWARD_EXCHANGES:
        todo = [(p, tf) for p in pairs for tf in tfs if not prior.get((p, tf), {}).get("oldest_reached")]
        if todo:
            try:
                z = ft.archive(exchange)
            except Exception as e:
                print(f"backfill: {exchange}: archive: {type(e).__name__}: {e}", flush=True)
                z, ok = None, False
            if z:
                archived = import_archive(z, exchange, todo)
                ok = len(archived) == len(todo)
        ok = collect_forward(ft, exchange, pairs, tfs) and ok
    else:
        for tf in tfs:
            todo = [p for p in pairs if not prior.get((p, tf), {}).get("oldest_reached")]
            if todo:
                if ft.download(exchange, ["--prepend", "--timerange", f"{PREPEND_FLOORS.get(tf, PREPEND_FROM)}-", "-t", tf, "--pairs", *todo]):
                    prepended.update((p, tf) for p in todo)
                else:
                    ok = False
            ok = ft.download(exchange, ["-t", tf, "--pairs", *pairs]) and ok
    series = [(p, tf) for p in pairs for tf in tfs]

    rows = []
    for pair, tf in series:
        r = scan(series_file(exchange, pair, tf), tf)
        r.update(exchange=exchange, pair=pair, timeframe=tf,
                 oldest_reached=(pair, tf) in archived or reached(prior.get((pair, tf)), r["first_ts"], (pair, tf) in prepended))
        rows.append(r)
    with conn.transaction():
        conn.cursor().executemany(UPSERT, rows)
        # A pair or timeframe dropped from the config is no longer backfilled; its row would only look stale.
        keep = [f"{p}|{tf}" for p, tf in series]
        conn.execute("DELETE FROM data_coverage WHERE exchange = %s AND NOT (pair || '|' || timeframe = ANY(%s))",
                     (exchange, keep))
    done = sum(r["oldest_reached"] for r in rows)
    newest = max((r["last_ts"] for r in rows if r["last_ts"]), default=None)
    print(f"backfill: {exchange}: {len(rows)} series, {done} at the oldest candle, newest {newest}, ok={ok}", flush=True)
    return ok


def run_one(ft: Freqtrade, exchange: str) -> bool:
    try:
        with connect(autocommit=True) as conn:
            return backfill_exchange(ft, conn, exchange, pairs_of(exchange), TIMEFRAMES)
    except Exception as e:  # one exchange's outage must not hide the others' results
        print(f"backfill: {exchange}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return False


def main(ft: Freqtrade | None = None) -> int:
    ft = ft or Cli()
    with ThreadPoolExecutor(max_workers=len(EXCHANGES)) as pool:
        results = dict(zip(EXCHANGES, pool.map(lambda ex: run_one(ft, ex), EXCHANGES)))
    failed = [ex for ex, ok in results.items() if not ok]
    if failed:
        print(f"backfill: download failures on {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
