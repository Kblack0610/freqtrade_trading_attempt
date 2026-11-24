#!/usr/bin/env python3
"""One sweep per venue: open markets and their top of book, change-only, plus the markets resolved since the last run.

  python -m lab.stages.recorder [venue ...]     # default: [recorder] default_venues in lab/events/markets.toml

For each venue it reads every open market a page at a time and diffs each outcome's quote against the last one stored
in Postgres (outcomes). Only the changed quotes are written, as one parquet object in MinIO, and only then is the
Postgres state moved forward in one transaction, so a run that dies midway writes nothing it has not also recorded.
If the database write fails after the object landed, the next run writes those changes again with a later ts;
readers keep the first row of a run of equal quotes. The resolution walk gets resolutions_budget_s: one that runs
past it stops at a page and the next sweep resumes it (migration 064), so a venue that re-stamps many old markets
cannot keep every sweep past its Job's deadline. The only writer of venues, markets, outcomes, resolutions and
market_sweeps (migration 019), and of the quote objects. Exits 1 if any venue failed; the others still record.
"""

import datetime as dt
import sys
import time
import tomllib
import traceback
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import fs as pafs

from lab.core.db import connect
from lab.events import CONFIG
from lab.events.kalshi import Kalshi
from lab.events.manifold import Manifold
from lab.sports.odds import OddsApi
from lab.events.polymarket import Polymarket, PolymarketUS
from lab.events.predictit import PredictIt
from lab.market import Quote, Venue
from lab.core.util import s3_from_env

ADAPTERS = {a.name: a for a in (Kalshi, PolymarketUS, Polymarket, PredictIt, Manifold, OddsApi)}
META = ("event_id", "series_id", "title", "subtitle", "market_type", "status", "open_time", "close_time")
BOOK = ("bid", "ask", "last", "bid_size", "ask_size", "volume", "open_interest")
SCHEMA = pa.schema([
    ("venue", pa.string()), ("market_id", pa.string()), ("outcome", pa.string()),
    ("ts", pa.timestamp("us", tz="UTC")), ("status", pa.string()),
    *[(f, pa.float64()) for f in BOOK],
])


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text())


def state(conn, venue: str, market_ids: list[str]) -> tuple[dict, dict]:
    """Read only the current API page; historical venue state can exceed the pod's memory."""
    meta = {r[0]: tuple(r[1:]) for r in conn.execute(
        f"SELECT market_id, {', '.join(META)} FROM markets WHERE venue = %s AND market_id = ANY(%s)",
        (venue, market_ids))}
    book = {(r[0], r[1]): tuple(r[2:]) for r in conn.execute(
        f"SELECT market_id, outcome, {', '.join(BOOK)} FROM outcomes WHERE venue = %s AND market_id = ANY(%s)",
        (venue, market_ids))}
    return meta, book


def new_resolutions(conn, venue: str, resolved) -> list:
    """Each market once, its first (newest) read, unless its resolution is stored and its market settled. A venue can
    re-stamp markets it settled long ago (Polymarket US's updatedAt, docs/markets.md), and writing each again row by
    row can outlast the Job, so no good sweep is logged, the lookback never moves and every later sweep re-reads them.
    One lookup of about 175k ids took 8 s on the live tables."""
    fresh = {}
    for r in resolved:
        fresh.setdefault(r.market_id, r)
    done = {row[0] for row in conn.execute(
        "SELECT r.market_id FROM resolutions r JOIN markets m USING (venue, market_id) "
        "WHERE r.venue = %s AND r.market_id = ANY(%s) AND m.status = 'settled'", (venue, list(fresh)))}
    return [r for m, r in fresh.items() if m not in done]


def write_quotes(fs: pafs.FileSystem, root: str, venue: str, started: dt.datetime, quotes: list[Quote]) -> str:
    key = f"{root}/venue={venue}/date={started:%Y-%m-%d}/{started:%H%M%S}Z.parquet"
    cols = {"venue": [venue] * len(quotes), "market_id": [q.market_id for q in quotes],
            "outcome": [q.outcome for q in quotes], "ts": [q.ts for q in quotes], "status": [q.status for q in quotes],
            **{f: [getattr(q, f) for q in quotes] for f in BOOK}}
    if fs.type_name == "local":  # an object store has no directories to make
        fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
    pq.write_table(pa.table(cols, schema=SCHEMA), key, filesystem=fs, compression="zstd")
    return key


def walk_resolutions(conn, v: Venue, started: dt.datetime, lookback: dt.timedelta, budget_s: float):
    """The resolutions this sweep reads and where its walk stopped: (resolved, since, cursor, walk_started). With no
    walk left unfinished it starts one at the start of the last finished walk, less `lookback`. With one, it first
    reads what settled since that walk began (the head of the list), then resumes it; a cursor the venue no longer
    takes starts that walk again from the newest. Past `budget_s` a walk stops after a page, with the cursor the next
    sweep resumes from."""
    last = conn.execute("SELECT started_at, walk_since, walk_cursor, walk_started FROM market_sweeps "
                        "WHERE venue = %s AND ok ORDER BY started_at DESC LIMIT 1", (v.name,)).fetchone()
    deadline = time.monotonic() + budget_s

    def walk(since, cursor):
        read, nxt = [], None
        for page, nxt in v.resolutions(since, cursor):
            read += page
            if nxt is not None and time.monotonic() > deadline:
                break
        return read, nxt

    if not (last and last[2]):
        since = ((last[3] or last[0]) if last else started) - lookback
        read, nxt = walk(since, None)
        return new_resolutions(conn, v.name, read), since, nxt, started
    since, cursor, top = last[1], last[2], last[3]
    head, _ = walk(top - lookback, None)
    try:
        tail, nxt = walk(since, cursor)
    except Exception:
        traceback.print_exc()
        print(f"markets: {v.name}: the resolution walk's cursor failed; walking it again from the newest", flush=True)
        tail, nxt = walk(since, None)
    return new_resolutions(conn, v.name, head + tail), since, nxt, top


def sweep(v: Venue, vcfg: dict, conn, fs: pafs.FileSystem, root: str, lookback: dt.timedelta,
          now: dt.datetime | None = None, budget_s: float = float("inf")) -> dict:
    started = now or dt.datetime.now(dt.timezone.utc)
    seen, new_markets, new_meta, changed = 0, 0, {}, []
    for page in v.open_markets():
        meta, book = state(conn, v.name, list({q.market_id for q in page}))
        for q in page:
            seen += 1
            m = tuple(getattr(q, f) for f in META)
            if meta.get(q.market_id) != m and q.market_id not in new_meta:
                new_meta[q.market_id] = q
                new_markets += q.market_id not in meta
            if book.get((q.market_id, q.outcome)) != tuple(getattr(q, f) for f in BOOK):
                changed.append(q)

    resolved, since, cursor, top = walk_resolutions(conn, v, started, lookback, budget_s)

    key = write_quotes(fs, root, v.name, started, changed) if changed else None
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("INSERT INTO venues (venue, title, kind, url) VALUES (%s, %s, %s, %s) ON CONFLICT (venue) DO "
                    "UPDATE SET title = EXCLUDED.title, kind = EXCLUDED.kind, url = EXCLUDED.url",
                    (v.name, vcfg.get("title", v.name), vcfg.get("kind", "exchange"), vcfg.get("url")))
        cur.executemany(
            f"""INSERT INTO markets (venue, market_id, {', '.join(META)}, first_seen, updated_at)
                VALUES (%s, %s, {', '.join(['%s'] * len(META))}, %s, %s)
                ON CONFLICT (venue, market_id) DO UPDATE SET
                {', '.join(f'{f} = EXCLUDED.{f}' for f in META)}, updated_at = EXCLUDED.updated_at""",
            [(v.name, q.market_id, *(getattr(q, f) for f in META), q.ts, q.ts) for q in new_meta.values()])
        cur.executemany(
            f"""INSERT INTO outcomes (venue, market_id, outcome, {', '.join(BOOK)}, quoted_at)
                VALUES (%s, %s, %s, {', '.join(['%s'] * len(BOOK))}, %s)
                ON CONFLICT (venue, market_id, outcome) DO UPDATE SET
                {', '.join(f'{f} = EXCLUDED.{f}' for f in BOOK)}, quoted_at = EXCLUDED.quoted_at""",
            [(v.name, q.market_id, q.outcome, *(getattr(q, f) for f in BOOK), q.ts) for q in changed])
        cur.executemany(
            "INSERT INTO resolutions (venue, market_id, result, value, settled_at, volume) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (venue, market_id) DO NOTHING",
            [(v.name, r.market_id, r.result, r.value, r.settled_at, r.volume) for r in resolved])
        cur.executemany("UPDATE markets SET status = 'settled', updated_at = %s WHERE venue = %s AND market_id = %s "
                        "AND status <> 'settled'", [(r.settled_at, v.name, r.market_id) for r in resolved])
        stats = {"seen": seen, "new_markets": new_markets, "changes": len(changed),
                 "resolved": len(resolved), "object_key": key, "walk_cursor": cursor}
        cur.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, "
                    "object_key, walk_cursor, walk_since, walk_started, ok) "
                    "VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, true)",
                    (v.name, started, *stats.values(), since, top))
    return stats


def main(argv: list[str] | None = None, fs: pafs.FileSystem | None = None, adapters: dict | None = None) -> int:
    cfg = load_config()
    adapters = adapters or ADAPTERS
    rec = cfg["recorder"]
    names = (argv if argv is not None else sys.argv[1:]) or rec.get("default_venues") or list(cfg["venues"])
    fs = fs or s3_from_env()
    root = f"{rec['bucket']}/{rec['prefix']}"
    lookback = dt.timedelta(hours=rec.get("resolutions_lookback_h", 1))
    budget_s = rec.get("resolutions_budget_s", float("inf"))
    failed = []
    with connect(autocommit=True) as conn:
        for name in names:
            started = dt.datetime.now(dt.timezone.utc)
            try:
                stats = sweep(adapters[name](cfg["venues"][name]), cfg["venues"][name], conn, fs, root, lookback, started,
                              budget_s)
                print(f"markets: {name}: {stats}", flush=True)
            except Exception as e:  # one venue down must not stop the rest
                traceback.print_exc()
                failed.append(name)
                conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, "
                             "resolved, ok, error) VALUES (%s, %s, now(), 0, 0, 0, 0, false, %s)",
                             (name, started, f"{type(e).__name__}: {e}"[:1000]))
    if failed:
        print(f"markets: failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
