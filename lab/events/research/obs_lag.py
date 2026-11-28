"""By hand: how long Kalshi takes to reprice a daily weather market once a station's report has decided it.

  python -m lab.events.research.obs_lag --since 2026-11-06 [--until 2026-11-28] [--edge 0.03]

The `observed` family (lab/events/families.py) can trade only while the price lags what the station has measured, so
this lag says how often nws_obs must be read. For each settled Kalshi market of a high, low or rain series of
lab/inputs/inputs.toml whose close falls in [since, until):

- the deciding report is the first IEM ASOS observation (lab/inputs/nws.py asos, the archive the backfill reads) of
  the market's day after which `settled_by` decides it, with the family's `margin`;
- the reprice is the first recorder quote at or after that report that the family could no longer trade: an invalid
  quote (0 < bid <= ask < 1 fails, as at 0/0.01) or no side beating the settled price by `edge` (default the smallest
  of the family's grid). The recorder writes a quote only when it changes, at most once a sweep (about 5 minutes).

Each market lands in one bucket: `lagged` (the minutes from report to reprice), `ahead` (the quote standing at the
report was already untradeable; its minutes, negative, go back to the quote that ended its last tradeable run, and it
has none when no quote of the window was tradeable), `held` (still tradeable at its last quote), `missing` (no quote at
or before the report), `undecided` (no report of the day decides it) or `wrong` (the report's side lost at
settlement); a void market is left out. Reads `markets`, `resolutions`, the recorder's quote objects and IEM; its only write is the IEM client's
pacing row (lab/core/budget.py), so it needs the jobs' database role.
"""

import argparse
import datetime as dt
import statistics
import sys
from collections import Counter, defaultdict

import pandas as pd

from lab import inputs
from lab.core.db import connect
from lab.core.util import s3_from_env
from lab.events import backtest
from lab.events.families import day_of, degrees, quote, settled_by, side_for, station_of, weather_markets
from lab.inputs import nws
from lab.stages.recorder import load_config

UTC = dt.timezone.utc
VENUE = "kalshi"
BUCKETS = ("lagged", "ahead", "held", "missing", "undecided", "wrong")


def deciding(obs: list[tuple], st: dict, kind: str, rng, day: dt.date, margin: int) -> tuple[dt.datetime, float] | None:
    """(report time, settled yes price) of the first observation of `day` after which the day so far decides the
    market, or None when no report of the day does."""
    for t, summary in nws.running(obs, st, dt.timedelta(0)):
        if summary["day"] == day.isoformat() and (fair := settled_by(kind, rng, summary, margin)) is not None:
            return t, fair
    return None


def tradeable(fair: float, bid: float, ask: float, edge: float) -> bool:
    q = quote(pd.Series({"bid": bid, "ask": ask}))
    return q is not None and side_for(fair, *q, edge) is not None


def reprice(quotes: pd.DataFrame, at: dt.datetime, fair: float, edge: float) -> tuple[str, float | None]:
    """The bucket and its minutes from the report `at`: lagged, to the first untradeable quote after it; ahead, back
    to the first quote of the untradeable run standing at it (None when the window has no tradeable quote). `quotes`
    are one market's ts, bid, ask, oldest first."""
    at = pd.Timestamp(at)
    before, after = quotes[quotes["ts"] <= at], quotes[quotes["ts"] > at]
    if before.empty:
        return "missing", None
    ok = [tradeable(fair, r.bid, r.ask, edge) for r in before.itertuples()]
    if not ok[-1]:
        run = [i for i, t in enumerate(ok) if t]
        return "ahead", None if not run else (before["ts"].iloc[run[-1] + 1] - at).total_seconds() / 60
    for r in after.itertuples():
        if not tradeable(fair, r.bid, r.ask, edge):
            return "lagged", (r.ts - at).total_seconds() / 60
    return "held", None


def measure(markets: list[tuple], obs: dict[str, list[tuple]], book: pd.DataFrame, reg: dict, margin: int,
            edge: float) -> list[tuple[str, str, float | None]]:
    """(kind, bucket, minutes) per market. `markets` are (market_id, event_id, series_id, subtitle, result) and `obs`
    each station's observations (nws.asos shape)."""
    wm, stations = weather_markets(reg), reg["nws_high"]["stations"]
    by = {m: g for m, g in book.groupby("market_id", sort=False)}
    out = []
    for market_id, event_id, series_id, subtitle, result in markets:
        hit, day = station_of(wm, series_id, market_id), day_of(event_id)
        if hit is None or day is None:
            continue
        key, kind = hit
        rng = None if kind == "rain" else degrees(subtitle)
        if kind != "rain" and rng is None:
            continue
        if result not in ("yes", "no"):
            continue
        got = deciding(obs.get(key, []), stations[key], kind, rng, day, margin)
        if got is None or got[1] != (1.0 if result == "yes" else 0.0):
            out.append((kind, "undecided" if got is None else "wrong", None))
            continue
        q = by.get(market_id)
        bucket, minutes = ("missing", None) if q is None else reprice(q, got[0], got[1], edge)
        out.append((kind, bucket, minutes))
    return out


def report(rows: list[tuple[str, str, float | None]]) -> list[str]:
    """One line per kind and one for all: the lagged markets' median, p25 and p90 minutes, how far ahead the ahead
    markets were, and every bucket's count."""
    by = defaultdict(list)
    for kind, bucket, minutes in rows:
        by[kind].append((bucket, minutes))
        by["all"].append((bucket, minutes))
    lines = []
    for kind in [k for k in ("high", "low", "rain", "all") if k in by]:
        n = Counter(b for b, _ in by[kind])
        lag = sorted(m for b, m in by[kind] if b == "lagged")
        stats = "no lagged market"
        if lag:
            q = statistics.quantiles(lag, n=20, method="inclusive") if len(lag) > 1 else [lag[0]] * 19
            stats = f"median {statistics.median(lag):.0f} min, p25 {q[4]:.0f}, p90 {q[17]:.0f}"
        ahead = [-m for b, m in by[kind] if b == "ahead" and m is not None]
        early = f"median {statistics.median(ahead):.0f} min before the report, " if ahead else ""
        lines.append(f"{kind}: {n['lagged']} lagged ({stats}); ahead {n['ahead']} ({early}"
                     f"{n['ahead'] - len(ahead)} never tradeable); " + ", ".join(f"{b} {n[b]}" for b in BUCKETS[2:]))
    return lines


def main(argv: list[str] | None = None, fs=None) -> int:
    cfg = load_config()
    fam = cfg["families"]["observed"]
    edges = fam["edge"] if isinstance(fam["edge"], list) else [fam["edge"]]
    ap = argparse.ArgumentParser(prog="python -m lab.events.research.obs_lag")
    ap.add_argument("--since", type=dt.date.fromisoformat, required=True, help="first close date (the recorder's first day)")
    ap.add_argument("--until", type=dt.date.fromisoformat, default=dt.datetime.now(UTC).date())
    ap.add_argument("--edge", type=float, default=min(edges))
    a = ap.parse_args(argv)
    reg = inputs.load()
    wm = weather_markets(reg)
    start, end = (dt.datetime.combine(d, dt.time(), UTC) for d in (a.since, a.until))
    with connect(autocommit=True) as conn:
        rows = conn.execute(
            "SELECT m.market_id, m.event_id, m.series_id, m.subtitle, r.result FROM markets m JOIN resolutions r "
            "ON r.venue = m.venue AND r.market_id = m.market_id WHERE m.venue = %s AND m.close_time >= %s "
            "AND m.close_time < %s AND (m.series_id = ANY(%s) OR m.series_id = 'KXRAIN')",
            (VENUE, start, end, sorted(wm))).fetchall()
    markets = [r for r in rows if station_of(wm, r[2], r[0]) is not None]
    iem = nws.client(reg[nws.CLI]["api"], accept="application/json")
    keys = sorted({station_of(wm, r[2], r[0])[0] for r in markets})
    # A local day west of UTC starts the UTC day before; a market closes the morning after its day.
    obs = {k: nws.asos(iem, k, reg[nws.NAME]["stations"][k], a.since - dt.timedelta(days=2), a.until) for k in keys}
    fs = fs or s3_from_env()
    root = f"{cfg['recorder']['bucket']}/{cfg['recorder']['prefix']}"
    # Recording is change-only: a week back finds the quote standing at a report early in the range.
    book = backtest.load_quotes(fs, root, VENUE, a.since - dt.timedelta(days=7), a.until + dt.timedelta(days=1),
                                [r[0] for r in markets])
    out = measure(markets, obs, book, reg, fam["margin"], a.edge)
    print(f"obs_lag: {len(markets)} settled markets closing {a.since} to {a.until}, {len(keys)} stations, "
          f"edge {a.edge}, margin {fam['margin']}")
    print("\n".join(report(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
