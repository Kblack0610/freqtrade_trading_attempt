#!/usr/bin/env python3
"""Fold the recorder's quote parts into one object per venue and day.

  python -m lab.markets.compact [venue ...]     # default: every venue= directory under the recorder prefix

The recorder writes one part per sweep, <prefix>/venue=<v>/date=<d>/<HHMMSSZ>.parquet, up to 288 a venue a day. Once
a day is settled (its end plus [compact] grace_h is past, so no sweep still writes into it), this streams that day's
parts, one at a time, after the rows of any earlier day.parquet, into date=<d>/day.parquet, then deletes the parts.
The day object names the parts it holds in its parquet metadata (key "parts"), so a run that dies between the
write and the deletes only deletes them next time, and a part that lands late is folded in the next night. Until
those deletes run a reader sees the rows twice; backtest.load_quotes keeps the first row of a run of equal quotes.
Exits 1 if any venue failed; the others still compact.
"""

import datetime as dt
import sys
import traceback

import pyarrow.parquet as pq
from pyarrow import fs as pafs

from lab.markets.recorder import SCHEMA, load_config
from lab.util import s3_from_env

DAY = "day.parquet"
PARTS = b"parts"


def held(fs: pafs.FileSystem, key: str) -> list[str]:
    """The part names a day object says it holds."""
    meta = pq.read_metadata(key, filesystem=fs).metadata or {}
    return meta.get(PARTS, b"").decode().split(",") if meta.get(PARTS) else []


def compact_day(fs: pafs.FileSystem, d: str) -> dict:
    """Fold the parts in date directory d into d/day.parquet and delete them; a no-op when d holds no part."""
    files = {i.base_name: i.path for i in fs.get_file_info(pafs.FileSelector(d)) if i.type == pafs.FileType.File}
    day = f"{d}/{DAY}"
    old = held(fs, day) if DAY in files else []
    parts = sorted(n for n in files if n.endswith("Z.parquet"))
    new = [n for n in parts if n not in old]
    rows = 0
    if new:
        tmp = f"{day}.tmp"  # readers take only *.parquet, so a dead run's tmp is never read
        names = old + new
        schema = SCHEMA.with_metadata({PARTS: ",".join(names).encode()})
        with pq.ParquetWriter(tmp, schema, filesystem=fs, compression="zstd") as w:
            for path in ([day] if old else []) + [files[n] for n in new]:
                for b in pq.ParquetFile(path, filesystem=fs).iter_batches():
                    w.write_batch(b)
                    rows += b.num_rows
        fs.move(tmp, day)
    for n in parts:
        fs.delete_file(files[n])
    return {"parts": len(parts), "rows": rows}


def settled(date_dir: str, now: dt.datetime, grace: dt.timedelta) -> bool:
    d = dt.date.fromisoformat(date_dir.rsplit("date=", 1)[1])
    return now >= dt.datetime.combine(d, dt.time(), dt.timezone.utc) + dt.timedelta(days=1) + grace


def compact_venue(fs: pafs.FileSystem, root: str, venue: str, grace: dt.timedelta,
                  now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    dirs = sorted(i.path for i in fs.get_file_info(pafs.FileSelector(f"{root}/venue={venue}", allow_not_found=True))
                  if i.type == pafs.FileType.Directory and "date=" in i.base_name)
    days, parts, rows = 0, 0, 0
    for d in dirs:
        if not settled(d, now, grace):
            continue
        s = compact_day(fs, d)
        if s["parts"]:
            days, parts, rows = days + 1, parts + s["parts"], rows + s["rows"]
    return {"days": days, "parts": parts, "rows": rows}


def main(argv: list[str] | None = None, fs: pafs.FileSystem | None = None, now: dt.datetime | None = None) -> int:
    cfg = load_config()
    rec = cfg["recorder"]
    fs = fs or s3_from_env()
    root = f"{rec['bucket']}/{rec['prefix']}"
    grace = dt.timedelta(hours=cfg.get("compact", {}).get("grace_h", 1))
    names = (argv if argv is not None else sys.argv[1:]) or sorted(
        i.base_name.removeprefix("venue=") for i in fs.get_file_info(pafs.FileSelector(root, allow_not_found=True))
        if i.type == pafs.FileType.Directory and i.base_name.startswith("venue="))
    failed = []
    for name in names:
        try:
            print(f"markets: compact {name}: {compact_venue(fs, root, name, grace, now)}", flush=True)
        except Exception:  # one venue's bad object must not stop the rest
            traceback.print_exc()
            failed.append(name)
    if failed:
        print(f"markets: compact failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
