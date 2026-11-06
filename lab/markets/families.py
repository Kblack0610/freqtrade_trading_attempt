#!/usr/bin/env python3
"""Strategy families v1 for event contracts. Each is a backtest.Strategy, and its command backtests every variant
in its grid through backtest.run(), which registers the trial before it reads the result.

  python -m lab.markets.families {longshot,calibration,gap} [--venue V] [--start D] [--end D] [--book bars|quotes]

The book is the history backfill's bars of the markets that settled start to end (end exclusive), or the
recorder's quotes of those days. Knobs and grids are [families.<name>] in lab/markets.toml: a list is a grid
axis, a list of tables merges each table into the variant, anything else is fixed. Every family enters a market
at most once, on its first row that qualifies, and holds to resolution, and it skips a row whose spread is wider
than `max_spread`.

- longshot (favourite-longshot bias): within `hours` of close, buy `buy` (no fades longshots, yes backs
  favourites) when the yes mid is in [lo, hi].
- calibration (base rates): fit() counts, over the `train_days` before start, how often markets entered the same
  way resolved yes, per `key` (all of the venue, or each series) and price bucket of `width`, shrunk toward the
  bucket's own price by `prior` markets. A test row then buys yes when that rate beats the ask by `edge`, or no
  when the bid beats it by `edge`. Only resolutions settled before start are counted, and no row before start
  is traded, so no trade is sized on a result it could not have known.
- gap (cross-venue gaps): a market of the venue linked by a `source` rule (market_links, confirmed_by rule) to a
  `ref_venue` market buys yes when the reference's fair price beats its ask by `edge`, or no when its bid beats
  the fair price by `edge`. Fair is the reference's latest mid at or before the row; for a sportsbook (outcomes
  <book>:<team>) each book's implied odds are divided by their sum, which removes its margin, and the median
  book is fair. Rule links are confirmed when they are made, so a rule link later rejected by its resolution is
  still traded: that is what the rule would have done. LLM links confirm only by resolving, which is too late to
  trade, so gap takes no LLM source. Each source is its own variant, so results are reported per source.
- llm (forecaster): forward only. Its `cutoff` is the model's training cutoff, and backtest.replay refuses any
  book that starts before it; it has no command here and is scored by the paper league.
"""

import argparse
import datetime as dt
import itertools
import json
import math
import re
import statistics
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from lab.db import connect
from lab.markets import backtest
from lab.markets.backtest import Market, Order
from lab.markets.history import _midnight
from lab.markets.links import outcome_word
from lab.markets.recorder import load_config, s3_from_env

UTC = dt.timezone.utc
COMMANDS = ("longshot", "calibration", "gap")


def variants(fcfg: dict) -> list[dict]:
    """Every params dict of a family's grid, in the order the grid lists them."""
    axes = [[(k, x) for x in v] if isinstance(v, list) else [(k, v)] for k, v in fcfg.items()]
    out = []
    for combo in itertools.product(*axes):
        p = {}
        for k, v in combo:
            if isinstance(v, dict):
                p.update(v)
            else:
                p[k] = v
        out.append(p)
    return out


def quote(row) -> tuple[float, float] | None:
    """The row's bid and ask when both are prices."""
    b, a = float(row.bid), float(row.ask)
    return (b, a) if math.isfinite(b) and math.isfinite(a) and 0 < b <= a < 1 else None


def closing(row, m: Market, hours: float | None) -> bool:
    """Is the row inside the market's last `hours` before close (any open row when hours is None)?"""
    if m.close_time is None:
        return hours is None
    left = (pd.Timestamp(m.close_time) - row.ts).total_seconds()
    return left > 0 and (hours is None or left <= hours * 3600)


def side_for(fair: float, bid: float, ask: float, edge: float) -> str | None:
    if fair - ask >= edge:
        return "yes"
    if bid - fair >= edge:
        return "no"
    return None


class Family:
    """One entry per market, held to resolution."""
    name = ""
    cutoff = None

    def __init__(self, params: dict):
        self.params, self.seen = params, set()

    def entry(self, row, m: Market) -> tuple[float, float] | None:
        if row.market_id in self.seen or not closing(row, m, self.params.get("hours")):
            return None
        q = quote(row)
        return q if q and q[1] - q[0] <= self.params["max_spread"] else None

    def order(self, row, side: str | None) -> list[Order]:
        if side is None:
            return []
        self.seen.add(row.market_id)
        return [Order(row.market_id, side, self.params["stake"])]


class Longshot(Family):
    name = "longshot"

    def on_book(self, row, m):
        q, p = self.entry(row, m), self.params
        return self.order(row, p["buy"] if q and p["lo"] <= (q[0] + q[1]) / 2 <= p["hi"] else None)


def _key(m: Market, key: str) -> str:
    return m.series_id or "" if key == "series" else ""


def bucket(mid: float, width: float) -> int:
    """The price bucket of `width` a mid falls in; 1e-9 keeps 0.30 / 0.05 (5.999...) in bucket 6."""
    return min(int(mid / width + 1e-9), int(round(1 / width)) - 1)


def _paid(r) -> float | None:
    """What one yes contract paid, or None for a void or unknown result."""
    if r is None or r.result == "void":
        return None
    return r.value if r.value is not None else {"yes": 1.0, "no": 0.0}.get(r.result)


def fit(book: pd.DataFrame, markets: dict, resolutions: dict, before: dt.datetime, params: dict) -> dict:
    """(key, bucket) -> (markets, yes paid) over the markets that settled before `before`, each entered on its
    first row before `before` the way the strategy enters (module docstring)."""
    before, width = pd.Timestamp(before), params["width"]
    probe, table = Family(params), defaultdict(lambda: [0, 0.0])
    for row in backtest._book(book[book["ts"] < before]).itertuples(index=False):
        m, r = markets.get(row.market_id), resolutions.get(row.market_id)
        paid = _paid(r)
        if m is None or paid is None or pd.Timestamp(r.settled_at) >= before or not (q := probe.entry(row, m)):
            continue
        probe.seen.add(row.market_id)
        t = table[(_key(m, params["key"]), bucket((q[0] + q[1]) / 2, width))]
        t[0], t[1] = t[0] + 1, t[1] + paid
    return {k: tuple(v) for k, v in table.items()}


class Calibration(Family):
    name = "calibration"

    def __init__(self, params: dict, table: dict, before: dt.datetime):
        super().__init__(params)
        self.table, self.before = table, pd.Timestamp(before)

    def rate(self, key: str, mid: float) -> float | None:
        p, w = self.params, self.params["width"]
        b = bucket(mid, w)
        n, paid = self.table.get((key, b), (0, 0.0))
        return None if n < p["min_n"] else (paid + p["prior"] * (b + 0.5) * w) / (n + p["prior"])

    def on_book(self, row, m):
        if row.ts < self.before or not (q := self.entry(row, m)):
            return []
        fair = self.rate(_key(m, self.params["key"]), (q[0] + q[1]) / 2)
        return self.order(row, None if fair is None else side_for(fair, *q, self.params["edge"]))


class RefBook:
    """The fair yes price of each linked outcome on a reference venue, as it stood at any time."""

    def __init__(self, quotes: pd.DataFrame, markets: dict):
        self.markets, self.series = markets, {}
        q = quotes.assign(ts=pd.to_datetime(quotes["ts"], utc=True)).sort_values(["market_id", "ts"], kind="stable")
        for mid, g in q.groupby("market_id", sort=False):
            latest, words, out = {}, set(), defaultdict(lambda: ([], []))
            for ts, rows in g.groupby("ts", sort=True):
                for r in rows.itertuples(index=False):
                    latest[r.outcome] = (r.bid, r.ask)
                    words.add(outcome_word(r.outcome))
                for w, f in self.fairs(latest, words).items():
                    out[w][0].append(ts.value)
                    out[w][1].append(f)
            for w, (t, f) in out.items():
                self.series[(mid, w)] = (np.array(t, dtype=np.int64), np.array(f))

    @staticmethod
    def fairs(latest: dict, words: set) -> dict:
        books, plain = defaultdict(dict), {}
        for o, (b, a) in latest.items():
            if ":" in o:
                if a is not None and math.isfinite(a) and a > 0:
                    book, w = o.rsplit(":", 1)
                    books[book][w] = a
            elif all(x is not None and math.isfinite(x) for x in (b, a)) and 0 < b <= a < 1:
                plain[o] = (b + a) / 2
        devig = defaultdict(list)
        for prices in books.values():
            if set(prices) == words:  # a book missing an outcome cannot be devigged
                total = sum(prices.values())
                for w, a in prices.items():
                    devig[w].append(a / total)
        return {**plain, **{w: statistics.median(v) for w, v in devig.items()}}

    def fair(self, market_id: str, word: str, at: pd.Timestamp) -> float | None:
        s, m = self.series.get((market_id, word)), self.markets.get(market_id)
        if s is None or (m and m.close_time and at >= pd.Timestamp(m.close_time)):
            return None
        i = int(np.searchsorted(s[0], at.value, side="right")) - 1
        return float(s[1][i]) if i >= 0 else None


def load_links(conn, venue: str, ref_venue: str, source: str) -> dict:
    """market_id on venue -> (ref market_id, ref outcome word), for each yes outcome a `source` rule linked to
    exactly one reference outcome. Rule links only (module docstring)."""
    by = defaultdict(set)
    for va, ma, oa, vb, mb, ob in conn.execute(
            "SELECT venue_a, market_id_a, outcome_a, venue_b, market_id_b, outcome_b FROM market_links "
            "WHERE source = %s AND confirmed_by = 'rule' AND ((venue_a = %s AND venue_b = %s) "
            "OR (venue_a = %s AND venue_b = %s))", (source, venue, ref_venue, ref_venue, venue)):
        (m, o), ref = ((ma, oa), (mb, ob)) if va == venue else ((mb, ob), (ma, oa))
        if o == "yes":
            by[m].add(ref)
    return {m: next(iter(refs)) for m, refs in by.items() if len(refs) == 1}


class Gap(Family):
    name = "gap"

    def __init__(self, params: dict, links: dict, ref: RefBook):
        if not params["source"].startswith("rule:"):
            raise ValueError(f"gap trades rule links only, not {params['source']}: an LLM link confirms only "
                             "when it resolves")
        super().__init__(params)
        self.links, self.ref = links, ref

    def on_book(self, row, m):
        link = self.links.get(row.market_id)
        if link is None or not (q := self.entry(row, m)):
            return []
        fair = self.ref.fair(*link, row.ts)
        return self.order(row, None if fair is None else side_for(fair, *q, self.params["edge"]))


FORECAST = ("You forecast the probability that a prediction market resolves yes, from its question alone. "
            'Reply with one JSON object and nothing else: {"p": <probability from 0 to 1>}.')


class Forecaster(Family):
    """An LLM's probability against the book. Forward only: refused on any book that starts before `cutoff`, the
    model's training cutoff, because its forecasts of earlier events are lookahead. `ask` takes (system, prompt)
    and returns the reply text."""
    name = "llm"

    def __init__(self, params: dict, ask, cutoff: dt.datetime):
        if cutoff is None:
            raise ValueError("an LLM forecaster needs its model's training cutoff")
        super().__init__(params)
        self.ask, self.cutoff, self.p = ask, cutoff, {}

    def forecast(self, m: Market) -> float | None:
        if m.market_id not in self.p:
            reply = self.ask(FORECAST, json.dumps({"question": m.title, "detail": m.subtitle,
                                                   "closes": m.close_time.isoformat() if m.close_time else None}))
            hit = re.search(r'"p"\s*:\s*([0-9.]+)', reply or "")
            p = float(hit[1]) if hit else None
            self.p[m.market_id] = p if p is not None and 0 <= p <= 1 else None
        return self.p[m.market_id]

    def on_book(self, row, m):
        if not (q := self.entry(row, m)) or (fair := self.forecast(m)) is None:
            return []
        return self.order(row, side_for(fair, *q, self.params["edge"]))


def main(argv: list[str] | None = None, fs=None) -> int:
    ap = argparse.ArgumentParser(prog="markets.families")
    ap.add_argument("family", choices=COMMANDS)
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat, help="exclusive; default today")
    ap.add_argument("--book", choices=("bars", "quotes"), default="bars")
    a = ap.parse_args(argv)
    cfg = load_config()
    fam, fcfg = cfg["families"], cfg["families"][a.family]
    end = a.end or dt.datetime.now(UTC).date()
    start = a.start or end - dt.timedelta(days=fam["days"])
    fs = fs or s3_from_env()
    bucket, bshare = cfg["recorder"]["bucket"], cfg["backtest"]["bar_volume_share"]

    def load(venue: str, s: dt.date, e: dt.date) -> pd.DataFrame:
        if a.book == "bars":
            return backtest.load_bars(fs, f"{bucket}/{cfg['history']['prefix']}/bars", venue, s, e, bshare)
        return backtest.load_quotes(fs, f"{bucket}/{cfg['recorder']['prefix']}", venue, s, e)

    book = load(a.venue, start, end)
    print(f"families: {a.family} {a.venue} {start} to {end}: {len(book)} rows, "
          f"{book['market_id'].nunique()} markets", flush=True)
    with connect(autocommit=True) as conn:
        train, refs = {}, {}
        for params in variants(fcfg):
            if a.family == "longshot":
                strategy = Longshot(params)
            elif a.family == "calibration":
                if params["train_days"] not in train:
                    tb = load(a.venue, start - dt.timedelta(days=params["train_days"]), start)
                    train[params["train_days"]] = (tb, *backtest.load_markets(conn, a.venue, tb["market_id"].unique()))
                table = fit(*train[params["train_days"]], _midnight(start), params)
                strategy = Calibration(params, table, _midnight(start))
            else:
                key = (params["ref_venue"], params["source"])
                if key not in refs:
                    links = load_links(conn, a.venue, *key)
                    rq = backtest.read_quotes(fs, f"{bucket}/{cfg['recorder']['prefix']}", params["ref_venue"],
                                              start - dt.timedelta(days=1), end)
                    rq = rq[rq["market_id"].isin({r[0] for r in links.values()})]
                    rm, _ = backtest.load_markets(conn, params["ref_venue"], rq["market_id"].unique())
                    refs[key] = (links, RefBook(rq, rm))
                strategy = Gap(params, *refs[key])
            out = backtest.run(conn, strategy, a.venue, book, _midnight(start), _midnight(end), cfg)
            print(f"families: {a.family} {backtest.variant_of(params)[:8]} {json.dumps(params, sort_keys=True)}: "
                  f"{out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
