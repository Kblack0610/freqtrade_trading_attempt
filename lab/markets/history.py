#!/usr/bin/env python3
"""History backfill: each venue's settled markets and how they paid, then their price bars and trades.

  python -m lab.markets.history settled [venue ...]   # markets and resolutions into Postgres
  python -m lab.markets.history bars [venue ...]      # price bars per settle date into parquet
  python -m lab.markets.history trades [venue ...]    # trades per settle date into parquet
      --budget-min N   stop between pages or dates once a run has taken N minutes ([history] budget_min)
      --restart        settled only: walk again from the newest market

With no venue named, a job runs its `[history] <job>_venues`. Each venue's knobs are `[history.<venue>]` in
lab/markets.toml; a `page_gap_s` there replaces the recorder's for these runs.

settled walks the venue's settled markets newest first, one page a transaction, saving the cursor that resumes the
walk in market_backfill. A market traded less than `settled_min_volume` is skipped. It writes markets (a row the
recorder already has is kept) and resolutions (on a row the recorder already has, only a missing volume is filled
in). A finished walk is not repeated: the recorder keeps resolutions current from then on.

bars and trades work one UTC settle date at a time, newest first: the date's resolutions whose volume reaches the
job's `min_volume`, within `since_days` and before today. A Kalshi date can hold 35k markets, more than one run can
fetch, so a date is fetched `chunk_markets` markets at a time, in market_id order, each chunk into its own part,
<bucket>/<prefix>/parts/<job>/venue=<v>/date=<d>/<first market's position>.parquet, and market_backfill saves the
position the next chunk starts at. A run stops between chunks once it is past its budget, and the next resumes the
date where it stopped. Once the last chunk is in, the parts stream into one object,
<bucket>/<prefix>/<job>/venue=<v>/date=<YYYY-MM-DD>.parquet, which an object store publishes only when it is closed
and a rerun overwrites, the parts are deleted, and the date is marked done with its market count. A date whose count
has changed since its first chunk (a later walk or the recorder added markets) starts over, done or not. Exits 1 if
any venue failed; the others still run.
"""

import argparse
import dataclasses
import datetime as dt
import sys
import time
import traceback

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import fs as pafs

from lab.db import connect
from lab.markets.recorder import ADAPTERS, META, load_config
from lab.markets.venue import Span
from lab.util import s3_from_env

TS = pa.timestamp("us", tz="UTC")
SCHEMAS = {
    "bars": pa.schema([("venue", pa.string()), ("market_id", pa.string()), ("ts", TS), ("period_s", pa.int32()),
                       *[(f, pa.float64()) for f in ("bid", "ask", "open", "high", "low", "close", "volume",
                                                     "open_interest")]]),
    "trades": pa.schema([("venue", pa.string()), ("market_id", pa.string()), ("trade_id", pa.string()), ("ts", TS),
                         ("price", pa.float64()), ("count", pa.float64()), ("taker_side", pa.string())]),
}
JOBS = ("settled", "bars", "trades")


def mark(conn, venue: str, job: str, unit: str, **cols) -> None:
    names = ", ".join(cols)
    conn.execute(f"INSERT INTO market_backfill (venue, job, unit, {names}, updated_at) "
                 f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(cols))}, now()) ON CONFLICT (venue, job, unit) DO UPDATE "
                 f"SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}, updated_at = now()",
                 (venue, job, unit, *cols.values()))


def walk_settled(v, vcfg: dict, conn, deadline: float, restart: bool = False, floor: float = 0) -> dict:
    row = conn.execute("SELECT cursor, done, rows FROM market_backfill WHERE venue = %s AND job = 'settled' "
                       "AND unit = ''", (v.name,)).fetchone()
    if row and row[1] and not restart:
        return {"done": True, "rows": row[2], "this_run": 0}
    cursor, total = (None, 0) if restart or not row else (row[0], row[2])
    conn.execute("INSERT INTO venues (venue, title, kind, url) VALUES (%s, %s, %s, %s) ON CONFLICT (venue) DO NOTHING",
                 (v.name, vcfg.get("title", v.name), vcfg.get("kind", "exchange"), vcfg.get("url")))
    n, nxt = 0, cursor
    for page, nxt in v.settled(cursor):
        page = [s for s in page if (s.resolution.volume or 0) >= floor]
        with conn.transaction():
            cur = conn.cursor()
            cur.executemany(
                f"""INSERT INTO markets (venue, market_id, {', '.join(META)}, first_seen, updated_at)
                    VALUES (%s, %s, {', '.join(['%s'] * len(META))}, now(), now())
                    ON CONFLICT (venue, market_id) DO NOTHING""",
                [(v.name, s.market.market_id, *(getattr(s.market, f) if f != "status" else "settled" for f in META))
                 for s in page])
            cur.executemany(
                "INSERT INTO resolutions (venue, market_id, result, value, settled_at, volume) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (venue, market_id) DO UPDATE "
                "SET volume = coalesce(resolutions.volume, EXCLUDED.volume)",
                [(v.name, r.market_id, r.result, r.value, r.settled_at, r.volume) for r in (s.resolution for s in page)])
            n += len(page)
            mark(cur, v.name, "settled", "", cursor=nxt, rows=total + n, done=nxt is None)
        if nxt is not None and time.monotonic() > deadline:
            return {"done": False, "rows": total + n, "this_run": n}
    if nxt is not None:  # a walk can end on an empty page before the venue says so
        mark(conn, v.name, "settled", "", cursor=None, rows=total + n, done=True)
    return {"done": True, "rows": total + n, "this_run": n}


def pending_dates(conn, venue: str, job: str, floor: float, since_days: int, today: dt.date) -> list[tuple[dt.date, int]]:
    start = today - dt.timedelta(days=since_days)
    counts = conn.execute(
        "SELECT (settled_at AT TIME ZONE 'UTC')::date AS d, count(*) FROM resolutions WHERE venue = %s "
        "AND coalesce(volume, 0) >= %s AND settled_at >= %s AND settled_at < %s GROUP BY 1 ORDER BY 1 DESC",
        (venue, floor, _midnight(start), _midnight(today))).fetchall()
    done = dict(conn.execute("SELECT unit, markets FROM market_backfill WHERE venue = %s AND job = %s AND done",
                             (venue, job)).fetchall())
    return [(d, n) for d, n in counts if done.get(d.isoformat()) != n]


def _midnight(d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(), dt.timezone.utc)


def spans(conn, venue: str, floor: float, d: dt.date) -> list[Span]:
    rows = conn.execute(
        "SELECT r.market_id, m.series_id, m.open_time, r.settled_at FROM resolutions r "
        "LEFT JOIN markets m USING (venue, market_id) WHERE r.venue = %s AND coalesce(r.volume, 0) >= %s "
        "AND r.settled_at >= %s AND r.settled_at < %s ORDER BY r.market_id",
        (venue, floor, _midnight(d), _midnight(d + dt.timedelta(days=1)))).fetchall()
    return [Span(*r) for r in rows]


def table(venue: str, recs: list, schema: pa.Schema) -> pa.Table:
    cols = {f.name: [getattr(r, f.name) for r in recs] for f in dataclasses.fields(recs[0])}
    return pa.table({"venue": [venue] * len(recs), **cols}, schema=schema)


def _clear(fs, d: str) -> None:
    if fs.get_file_info(d).type == pafs.FileType.Directory:
        fs.delete_dir(d)


def backfill_date(v, job: str, conn, fs, root: str, d: dt.date, floor: float, period_s: int, chunk: int,
                  deadline: float) -> dict:
    """Fetch the date's markets from where the last run stopped, a chunk at a time, until it is all in or the run is
    past its deadline; merge the parts into the date's object once it is all in."""
    unit, todo = d.isoformat(), spans(conn, v.name, floor, d)
    key = f"{root}/{job}/venue={v.name}/date={unit}.parquet"
    parts = f"{root}/parts/{job}/venue={v.name}/date={unit}"
    row = conn.execute("SELECT cursor, markets, rows FROM market_backfill WHERE venue = %s AND job = %s AND unit = %s "
                       "AND NOT done", (v.name, job, unit)).fetchone()
    pos, rows = (int(row[0]), row[2]) if row and row[0] and row[1] == len(todo) else (0, 0)
    if pos == 0:
        _clear(fs, parts)
    if fs.type_name == "local":
        fs.create_dir(parts, recursive=True)
    fetched = 0
    while pos < len(todo):
        with pq.ParquetWriter(f"{parts}/{pos:06d}.parquet", SCHEMAS[job], filesystem=fs, compression="zstd") as w:
            for s in todo[pos:pos + chunk]:
                for recs in ([v.bars(s, period_s)] if job == "bars" else v.trades(s)):
                    if recs:
                        w.write_table(table(v.name, recs, SCHEMAS[job]))
                        rows += len(recs)
        n = min(chunk, len(todo) - pos)
        pos, fetched = pos + n, fetched + n
        mark(conn, v.name, job, unit, cursor=str(pos), markets=len(todo), rows=rows, object_key=None, done=False)
        if pos < len(todo) and time.monotonic() > deadline:
            return {"date": unit, "markets": len(todo), "fetched": fetched, "rows": rows, "done": False}
    if fs.type_name == "local":
        fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
    names = sorted(i.path for i in fs.get_file_info(pafs.FileSelector(parts)) if i.path.endswith(".parquet"))
    with pq.ParquetWriter(key, SCHEMAS[job], filesystem=fs, compression="zstd") as w:
        for n in names:
            for b in pq.ParquetFile(n, filesystem=fs).iter_batches():
                w.write_batch(b)
    mark(conn, v.name, job, unit, cursor=None, markets=len(todo), rows=rows, object_key=key, done=True)
    _clear(fs, parts)
    return {"date": unit, "markets": len(todo), "fetched": fetched, "rows": rows, "done": True}


def backfill(v, job: str, hcfg: dict, conn, fs, root: str, deadline: float, today: dt.date | None = None) -> dict:
    floor = float(hcfg.get(f"{job}_min_volume", 0))
    todo = pending_dates(conn, v.name, job, floor, int(hcfg.get(f"{job}_since_days", 1095)),
                         today or dt.datetime.now(dt.timezone.utc).date())
    done, rows, period_s = 0, 0, int(hcfg.get("bars_period_min", 60)) * 60
    chunk = int(hcfg.get("chunk_markets", 500))
    for d, _ in todo:
        stats = backfill_date(v, job, conn, fs, root, d, floor, period_s, chunk, deadline)
        print(f"markets: history {job} {v.name}: {stats}", flush=True)
        done, rows = done + stats["done"], rows + stats["rows"]
        if time.monotonic() > deadline:
            break
    return {"dates": done, "left": len(todo) - done, "rows": rows}


def main(argv: list[str] | None = None, fs=None, adapters: dict | None = None) -> int:
    p = argparse.ArgumentParser(prog="markets.history")
    p.add_argument("job", choices=JOBS)
    p.add_argument("venues", nargs="*")
    p.add_argument("--budget-min", type=float)
    p.add_argument("--restart", action="store_true")
    a = p.parse_args(argv)
    cfg = load_config()
    h, adapters = cfg["history"], adapters or ADAPTERS
    deadline = time.monotonic() + 60 * (a.budget_min if a.budget_min is not None else h.get("budget_min", 50))
    root = f"{cfg['recorder']['bucket']}/{h['prefix']}"
    if a.job != "settled":
        fs = fs or s3_from_env()
    failed = []
    with connect(autocommit=True) as conn:
        for name in a.venues or h[f"{a.job}_venues"]:
            hcfg = h.get(name, {})
            try:
                v = adapters[name]({**cfg["venues"][name], **{k: hcfg[k] for k in ("page_gap_s",) if k in hcfg}})
                if not hasattr(v, a.job):
                    raise ValueError(f"{name} serves no {a.job} history")
                stats = (walk_settled(v, cfg["venues"][name], conn, deadline, a.restart,
                                      float(hcfg.get("settled_min_volume", 0))) if a.job == "settled"
                         else backfill(v, a.job, hcfg, conn, fs, root, deadline))
                print(f"markets: history {a.job} {name}: {stats}", flush=True)
            except Exception:  # one venue down must not stop the rest
                traceback.print_exc()
                failed.append(name)
    if failed:
        print(f"markets: history {a.job} failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
