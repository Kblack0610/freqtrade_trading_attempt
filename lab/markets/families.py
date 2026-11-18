#!/usr/bin/env python3
"""Strategy families v1 for event contracts. Each is a backtest.Strategy, and the command walks every variant in
each family's grid forward over the settled months, then gates each variant on the months it was not fitted on.

  python -m lab.markets.families [--venue V] [--family F ...] [--rerun]

Walk-forward: a window is one whole calendar month before the current one whose every settle date the history
backfill has finished (market_backfill, bars done), and its book is the bars of the markets that settled in it.
Each variant runs on each window as configured, through backtest.run(), which registers the window as a trial before
it reads the result and stores it as its own market_backtests row; calibration fits only on the `train_days` before
the window, which must be backfilled too, and a training book with a row in the window raises Lookahead. A stored
window is not run again unless --rerun. The newest `lockbox_months` windows are the lockbox. A run starts no
window after `budget_min`; the next resumes.

The gate (gate, verdict) pools each variant's windows before the lockbox and applies the suggest.toml [confidence]
thresholds that fit a betting variant: enough trades, the interval low end above zero net of fees with whole events
resampled, the deflated Sharpe and the minimum backtest length at the betting lane's trial count, PBO over the
family's variants (metrics.pbo_panel), and enough positive months. A variant that passes all of them replays the
lockbox once and passes only if it is positive there too. One readiness row per variant.

Knobs and grids are [families] and [families.<name>] in lab/markets.toml, expanded by lab/grid.py. Every family enters a market at most once, on its first row that qualifies, and holds to resolution, and it skips a row whose spread is wider
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
- tt_totals (head-to-head totals): runs on a sportsbook venue (kind sportsbook), whose book is line_book over
  its recorded totals quotes and whose windows are whole months it recorded (recorded_dates). Within `hours` of a
  game's start, once per game: features() of the pair's earlier finals against the line (meetings that did not
  push, their over-rate, the last five's, the mean total less the line), from Meetings, which shows only games
  settled before the row and raises Lookahead on a read of any other score. The over-rate shrunk by `prior`
  games toward the no-vig fair (RefBook.fairs) is the probability; buy the over or under when it beats the best
  book by `edge` and the last five and the mean agree. Its venue needs a totals adapter writing odds.py's shape;
  table tennis has none yet (docs/markets.md, Table tennis data).
- weather (NWS forecast): a Kalshi daily high market (a series of lab/inputs.toml nws_high, its range from the
  subtitle and its day from the event) against the station's NWS forecast high, the newest one read at or before the
  row and no older than its fresh_min (input_readings, lab/nws.py). The high is read as normal around the forecast
  with `sd`; buy yes or no when the range's chance beats the price by `edge`. NWS keeps no forecast archive, so a
  window runs only once every day of it has a reading (Loader.forecasts): the first is the first whole month read.
- llm (forecaster): forward only. Its `cutoff` is the model's training cutoff, and backtest.replay refuses any
  book that starts before it; it has no command here and is scored by the paper league.
"""

import argparse
import datetime as dt
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd
from pyarrow import fs as pafs

from psycopg.types.json import Jsonb

from lab import confidence, inputs, metrics, readiness, suggest, taxonomy
from lab.db import connect
from lab.grid import variants
from lab.market import Order, Resolution
from lab.markets import backtest
from lab.markets.backtest import Lookahead, Market
from lab.markets.history import _midnight
from lab.markets.links import LINE, outcome_word, pays
from lab.markets.recorder import load_config
from lab.suggest import gate as check
from lab.util import s3_from_env

UTC = dt.timezone.utc
LEAD = dt.timedelta(days=2)  # a line book reads quotes from this long before its window, for games early in it
COMMANDS = ("longshot", "calibration", "gap", "weather", "observed")
LINES = ("tt_totals",)  # a sportsbook venue's families, on its line book (line_book)


def categories() -> dict:
    return taxonomy.load()["market_categories"]


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
    """One entry per market, held to resolution.

    Traced (the paper league sets `checks` and `inputs` to lists before each row; a backtest leaves them None), each
    rule check is recorded as {name, value, threshold, pass} and each input it read as {name, value} (with as_of
    when the input is older than the row), and the checks run on past the first failure where they can, so the
    paper league sees a near-miss. A traced and an untraced strategy make the same orders. `CHECKS` names every
    check a full trace holds; `INPUTS` every input it reads, each an entry of lab/inputs.toml, which gives its source."""
    name = ""
    cutoff = None
    CHECKS: tuple = ("new market", "hours to close", "quote valid", "spread")
    INPUTS: tuple = ("bid", "ask", "yes_depth", "no_depth", "close_time")  # the book row's (paper.traced)

    def __init__(self, params: dict):
        self.params, self.seen = params, set()
        self.checks: list | None = None
        self.inputs: list | None = None
        self.quoted: tuple[float, float] | None = None

    def passes(self, name: str, value, threshold, ok: bool) -> bool:
        if self.checks is not None:
            self.checks.append({"name": name, "value": value, "threshold": threshold, "pass": bool(ok)})
        return bool(ok)

    def read(self, name: str, value, as_of=None) -> None:
        if name not in self.INPUTS:
            raise ValueError(f"{self.name} reads {name}, which it does not declare in INPUTS")
        if self.inputs is not None:
            self.inputs.append({"name": name, "value": value} | ({} if as_of is None else {"as_of": as_of}))

    def rule(self) -> str:
        """The family's rule in words, from its params."""
        return self.name

    def entry(self, row, m: Market) -> tuple[float, float] | None:
        self.quoted, p = None, self.params
        new = row.market_id not in self.seen
        if not self.passes("new market", new, True, new):
            return None
        timely = closing(row, m, p.get("hours"))
        if self.checks is None and not timely:
            return None
        if self.checks is not None:
            left = None if m.close_time is None else (pd.Timestamp(m.close_time) - row.ts).total_seconds() / 3600
            self.passes("hours to close", left, p.get("hours"), timely)
        self.quoted = q = quote(row)
        if not self.passes("quote valid", [float(row.bid), float(row.ask)], "0 < bid <= ask < 1", q is not None):
            return None
        tight = self.passes("spread", q[1] - q[0], p["max_spread"], q[1] - q[0] <= p["max_spread"])
        return q if timely and tight else None

    def order(self, row, side: str | None) -> list[Order]:
        if side is None:
            return []
        self.seen.add(row.market_id)
        return [Order(row.market_id, side, self.params["stake"])]


class Longshot(Family):
    name = "longshot"
    CHECKS = Family.CHECKS + ("mid in band",)

    def rule(self) -> str:
        p = self.params
        return f"longshot: buy {p['buy']} when the yes mid is {p['lo']:.2f}-{p['hi']:.2f}, within {p['hours']} h of close"

    def on_book(self, row, m):
        q, p = self.entry(row, m), self.params
        if (got := q or (self.checks is not None and self.quoted)) and self.passes(
                "mid in band", (got[0] + got[1]) / 2, [p["lo"], p["hi"]], p["lo"] <= (got[0] + got[1]) / 2 <= p["hi"]):
            return self.order(row, p["buy"] if q else None)
        return []


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


def line_resolution(line_id: str, r: Resolution) -> Resolution:
    """How a line market `<market>|<line>` (line_book) resolves, given its game's resolution: yes when the total went
    over, no under, void on a push or a void game."""
    line = line_id.rsplit("|", 1)[1]
    paid = None if r.result in ("void", "split") else pays(f"Over {line}", r.result)
    return Resolution(line_id, "void" if paid is None else "yes" if paid else "no",
                      None if paid is None else float(paid), r.settled_at, r.volume)


def line_book(quotes: pd.DataFrame, markets: dict, resolutions: dict, depth: float) -> tuple:
    """A sportsbook's totals markets (lab/markets/odds.py: outcomes <book>:Over 216.5, resolved `Total 220`) as one
    binary market per line, `<market>|<line>`, yes being the over. Its ask is the best book's over and its bid one
    minus the best book's under, so a no buys the under at that book's price with its margin in; a row needs both
    sides. Its event is the game, so the interval resamples a game's lines together. The line resolves yes, no, or
    void on a push (links.pays). Returns the book, its markets and resolutions, and the no-vig fair of the over at
    each row, (line market, ts ns) -> fair, from RefBook.fairs. Sportsbooks quote no size: each side shows `depth`."""
    rows, lm, lr, fair = [], {}, {}, {}
    q = quotes.assign(ts=pd.to_datetime(quotes["ts"], utc=True)).sort_values(["market_id", "ts"], kind="stable")
    for mid, g in q[q["market_id"].isin(set(markets))].groupby("market_id", sort=False):
        m, r, latest = markets[mid], resolutions.get(mid), defaultdict(dict)
        for ts, at in g.groupby("ts", sort=True):
            touched = set()
            for o in at.itertuples(index=False):
                hit = LINE.fullmatch(outcome_word(o.outcome))
                if hit and hit[1] in ("Over", "Under"):
                    latest[hit[2]][o.outcome] = (o.bid, o.ask)
                    touched.add(hit[2])
            for line in touched:
                words = {f"Over {line}", f"Under {line}"}
                best = {w: min((a for o, (_, a) in latest[line].items() if outcome_word(o) == w
                                and a is not None and math.isfinite(a) and a > 0), default=None) for w in words}
                if None in best.values():
                    continue
                lid = f"{mid}|{line}"
                if lid not in lm:
                    lm[lid] = Market(lid, mid, m.series_id, m.title, f"Over {line}", "total_line", m.open_time,
                                     m.close_time)
                    if r is not None:
                        lr[lid] = line_resolution(lid, r)
                rows.append((lid, ts, 1.0 - best[f"Under {line}"], best[f"Over {line}"], depth, depth))
                f = RefBook.fairs(latest[line], words).get(f"Over {line}")
                if f is not None:
                    fair[(lid, ts.value)] = f
    book = backtest._book(pd.DataFrame(rows, columns=backtest.BOOK)) if rows else backtest._empty()
    return book, lm, lr, fair


def pair(m: Market) -> tuple:
    """The two sides of a game, whichever is home: its sport and the names either side of " at " in its title."""
    return m.series_id, frozenset(m.title.rsplit(" at ", 1))


class Meetings:
    """The final totals of past games, by pair, as they stood at any time: a game counts only once it settled
    before the time asked about, and reading a score that had not settled by then raises Lookahead. Built from a
    sportsbook's resolved totals markets (`Total 220`)."""

    def __init__(self, markets: dict, resolutions: dict):
        self.by, self.final = defaultdict(list), {}
        for mid, r in resolutions.items():
            m, hit = markets.get(mid), LINE.fullmatch(r.result or "")
            if m is None or " at " not in m.title or not hit or hit[1] != "Total":
                continue
            at = pd.Timestamp(r.settled_at).value
            self.final[mid] = (at, float(hit[2]))
            self.by[pair(m)].append((at, float(hit[2])))
        self.by = {k: (np.array([a for a, _ in v], dtype=np.int64), np.array([t for _, t in v]))
                   for k, v in ((k, sorted(v)) for k, v in self.by.items())}

    def before(self, m: Market, at: pd.Timestamp) -> np.ndarray:
        """The pair's final totals settled before `at`, oldest first."""
        s = self.by.get(pair(m))
        return s[1][:int(np.searchsorted(s[0], at.value, side="left"))] if s else np.array([])

    def score(self, market_id: str, at: pd.Timestamp) -> float | None:
        f = self.final.get(market_id)
        if f is not None and f[0] >= at.value:
            raise Lookahead(f"the final score of {market_id} is read at {at}, before it settled")
        return None if f is None else f[1]


def meetings_of(conn, venue: str) -> Meetings:
    """Every resolved totals market of the venue as Meetings."""
    ids = [r[0] for r in conn.execute(
        "SELECT market_id FROM resolutions JOIN markets USING (venue, market_id) WHERE venue = %s "
        "AND market_type = 'totals'", (venue,))]
    return Meetings(*backtest.load_markets(conn, venue, ids))


def features(totals: np.ndarray, line: float) -> dict | None:
    """What tt_totals reads of a pair's past totals against a line: the meetings that did not push (h2h), the share
    of them over the line, that share over the last five, and the mean total less the line. None with no meeting."""
    decided = totals[totals != line]
    if not len(decided):
        return None
    last = decided[-5:]
    return {"h2h": len(decided), "over_rate": float(np.mean(decided > line)), "last5_rate": float(np.mean(last > line)),
            "mean_gap": float(np.mean(totals) - line)}


class TTTotals(Family):
    """Head-to-head totals: once per game, within `hours` of its start, on a line whose pair has met `min_h2h`
    times: the over-rate shrunk by `prior` games toward the no-vig fair is the probability, and it buys the over
    (yes) or the under (no) when that beats the price by `edge` and the last five and the mean total agree."""
    name = "tt_totals"
    CHECKS = ("new game",) + Family.CHECKS + ("head to head", "fair price", "edge", "form agrees")
    INPUTS = Family.INPUTS + ("head_to_head", "no_vig_fair")

    def __init__(self, params: dict, meetings: Meetings, fairs: dict):
        super().__init__(params)
        self.meetings, self.fairs = meetings, fairs

    def rule(self) -> str:
        p = self.params
        return (f"tt_totals: buy the over or the under when the pair's over-rate at the line, over {p['min_h2h']}+ "
                f"meetings shrunk by {p['prior']} games toward the no-vig fair, beats the price by {p['edge']}, and "
                f"the last five ({p['last5']} on that side) and the mean total agree, within {p['hours']} h of the start")

    def on_book(self, row, m):
        new = m.event_id not in self.seen
        if not self.passes("new game", new, True, new) or not (q := self.entry(row, m)):
            return []
        p, line = self.params, float(m.subtitle.split()[-1])
        f, fair = features(self.meetings.before(m, row.ts), line), self.fairs.get((row.market_id, row.ts.value))
        self.read("head_to_head", f)
        self.read("no_vig_fair", fair)
        if not self.passes("head to head", f and f["h2h"], p["min_h2h"], f is not None and f["h2h"] >= p["min_h2h"]) \
                or not self.passes("fair price", fair, "known", fair is not None):
            return []
        prob = (f["over_rate"] * f["h2h"] + p["prior"] * fair) / (f["h2h"] + p["prior"])
        side = side_for(prob, *q, p["edge"])
        if not self.passes("edge", {"prob": prob, "bid": q[0], "ask": q[1]}, p["edge"], side is not None):
            return []
        agree = {"yes": f["last5_rate"] >= p["last5"] and f["mean_gap"] > 0,
                 "no": 1 - f["last5_rate"] >= p["last5"] and f["mean_gap"] < 0}
        if not self.passes("form agrees", {"side": side, "last5_rate": f["last5_rate"], "mean_gap": f["mean_gap"]},
                           p["last5"], agree[side]):
            return []
        self.seen.add(m.event_id)
        return self.order(row, side)


TEMP = re.compile(r"(-?\d+)° (?:or (above|below)|to (-?\d+)°)")


def degrees(subtitle: str | None) -> tuple[int | None, int | None] | None:
    """The yes range of a temperature market, in whole degrees, from its subtitle: `57° or above` (57, None),
    `48° or below` (None, 48), `55° to 56°` (55, 56)."""
    hit = TEMP.fullmatch((subtitle or "").strip())
    if not hit:
        return None
    n = int(hit[1])
    return (n, None) if hit[2] == "above" else (None, n) if hit[2] == "below" else (n, int(hit[3]))


def day_of(event_id: str | None) -> dt.date | None:
    """The date a Kalshi daily event is for: KXHIGHNY-26NOV15 and KXRAINDNYC-261115 are 2026-11-15."""
    for form in ("%y%b%d", "%y%m%d"):
        try:
            return dt.datetime.strptime((event_id or "").rsplit("-", 1)[-1], form).date()
        except ValueError:
            pass
    return None


def chance(lo: int | None, hi: int | None, mean: float, sd: float) -> float:
    """P(lo <= high <= hi) for a whole-degree high read as normal around `mean` with `sd`, half a degree each side."""
    cdf = lambda x: 0.5 * (1 + math.erf((x - mean) / (sd * math.sqrt(2))))  # noqa: E731
    return (1.0 if hi is None else cdf(hi + 0.5)) - (0.0 if lo is None else cdf(lo - 0.5))


class Forecasts:
    """Each station's recorded NWS forecast (input_readings of nws_high: key, as_of, value) as it stood at any time:
    the newest reading as of at or before the time asked about, so no row sees a forecast made after it."""

    def __init__(self, rows: list[tuple]):
        by = defaultdict(list)
        for key, as_of, value in rows:
            by[key].append((pd.Timestamp(as_of).value, value))
        self.by = {k: (np.array([t for t, _ in v], dtype=np.int64), [x for _, x in v])
                   for k, v in ((k, sorted(v, key=lambda r: r[0])) for k, v in by.items())}

    def at(self, key: str, ts: pd.Timestamp) -> tuple[dict, pd.Timestamp] | None:
        s = self.by.get(key)
        i = -1 if s is None else int(np.searchsorted(s[0], ts.value, side="right")) - 1
        return None if i < 0 else (s[1][i], pd.Timestamp(int(s[0][i]), tz="UTC"))


class Weather(Family):
    """A Kalshi daily high market against the NWS forecast high of the station it settles on (lab/inputs.toml
    nws_high): the high read as normal around the forecast with `sd` degrees, the market's range has a chance, and
    it buys yes or no when that beats the price by `edge`. The forecast is the newest read at or before the row and
    no older than the input's fresh_min."""
    name = "weather"
    CHECKS = ("temperature market",) + Family.CHECKS + ("forecast known", "forecast fresh", "edge")
    INPUTS = Family.INPUTS + ("nws_high",)

    def __init__(self, params: dict, forecasts: Forecasts, reg: dict):
        super().__init__(params)
        e = reg["nws_high"]
        self.forecasts, self.fresh_min = forecasts, e["fresh_min"]
        self.stations = {s: key for key, st in e["stations"].items() for s in st["series"]}

    def rule(self) -> str:
        p = self.params
        return (f"weather: buy yes or no when the chance of the market's range, the high read as normal around the NWS "
                f"forecast high with sd {p['sd']} F, beats the price by {p['edge']}, within {p['hours']} h of close")

    def on_book(self, row, m):
        p, st = self.params, self.stations.get(m.series_id or "")
        day, rng = day_of(m.event_id), degrees(m.subtitle)
        if not self.passes("temperature market", m.series_id, "a series of lab/inputs.toml nws_high",
                           st is not None and day is not None and rng is not None):
            return []
        q = self.entry(row, m)
        if not (got := q or (self.checks is not None and self.quoted)):
            return []
        hit = self.forecasts.at(st, row.ts)
        high = None if hit is None else hit[0]["highs"].get(day.isoformat())
        self.read("nws_high", None if hit is None else {"station": st, "day": day.isoformat(), "high": high,
                                                         "grid": hit[0].get("grid")}, None if hit is None else hit[1])
        if not self.passes("forecast known", high, f"a forecast high for {day}", high is not None):
            return []
        age = (row.ts - hit[1]).total_seconds() / 60
        if not self.passes("forecast fresh", age, self.fresh_min, age <= self.fresh_min):
            return []
        prob = chance(*rng, high, p["sd"])
        side = side_for(prob, *got, p["edge"])
        if not self.passes("edge", {"prob": prob, "bid": got[0], "ask": got[1]}, p["edge"], side is not None):
            return []
        return self.order(row, side if q else None)


def settled_by(kind: str, rng: tuple | None, obs: dict, margin: int) -> float | None:
    """1.0 or 0.0 when what the station has measured already decides the market, else None. A day's high only grows
    and its low only falls, and the report rounds whole degrees, so `margin` degrees of slack keep a reading that
    rounds the other way from deciding it. Rain is decided only upward: measured rain cannot be undone, a dry day can
    still turn wet."""
    if kind == "rain":
        return 1.0 if obs["rained"] else None
    lo, hi = rng
    if kind == "high":
        high = obs["high"]
        if high is None:
            return None
        if hi is not None and high - margin > hi:
            return 0.0
        return 1.0 if hi is None and lo is not None and high - margin >= lo else None
    low = obs["low"]
    if low is None:
        return None
    if lo is not None and low + margin < lo:
        return 0.0
    return 1.0 if lo is None and hi is not None and low + margin <= hi else None


class Observed(Family):
    """A Kalshi daily weather market (high, low, or will it rain) the station's own readings of that day have already
    decided (settled_by): buys the side the observation settles while the price still lags it by `edge`. The
    reading is the newest at or before the row, of the market's own day and no older than the input's fresh_min, so a
    row never sees a reading made after it and a market is never bet before its day has an observation."""
    name = "observed"
    CHECKS = ("weather market",) + Family.CHECKS + ("observation known", "observation fresh", "settled by observation",
                                                    "edge")
    INPUTS = Family.INPUTS + ("nws_obs",)

    def __init__(self, params: dict, observations: Forecasts, reg: dict):
        super().__init__(params)
        e = reg["nws_high"]
        self.obs, self.fresh_min = observations, reg["nws_obs"]["fresh_min"]
        self.markets = {}  # series -> (station, kind); the rain event's series holds every city, told by its code
        for key, st in e["stations"].items():
            for s in st["series"]:
                self.markets[s] = (key, "high")
            self.markets[f"KXLOWT{st['code']}"] = (key, "low")
            self.markets[f"KXRAIND{st['code']}"] = (key, "rain")
            self.markets[f"KXRAIN-{st['code']}"] = (key, "rain")

    def rule(self) -> str:
        p = self.params
        return (f"observed: buy the side a station's own readings of the day have already settled (the high so far "
                f"above a range, the low so far below one, measured rain) while the price lags it by {p['edge']}, "
                f"within {p['hours']} h of close, {p['margin']} F of slack")

    def station(self, m: Market) -> tuple[str, str] | None:
        s = m.series_id or ""
        return self.markets.get(f"{s}-{m.market_id.rsplit('-', 1)[-1]}" if s == "KXRAIN" else s)

    def on_book(self, row, m):
        p, hit = self.params, self.station(m)
        day, rng = day_of(m.event_id), None if m.subtitle is None else degrees(m.subtitle)
        known = hit is not None and day is not None and (hit[1] == "rain" or rng is not None)
        if not self.passes("weather market", m.series_id, "a high, low or rain series of lab/inputs.toml", known):
            return []
        q = self.entry(row, m)
        if not (got := q or (self.checks is not None and self.quoted)):
            return []
        key, kind = hit
        seen = self.obs.at(key, row.ts)
        obs = None if seen is None or seen[0]["day"] != day.isoformat() else seen[0]
        self.read("nws_obs", None if obs is None else {"station": key, "day": obs["day"], "high": obs["high"],
                                                      "low": obs["low"], "rained": obs["rained"]},
                  None if obs is None else seen[1])
        if not self.passes("observation known", None if obs is None else obs["n"], f"an observation of {day}",
                           obs is not None):
            return []
        age = (row.ts - seen[1]).total_seconds() / 60
        if not self.passes("observation fresh", age, self.fresh_min, age <= self.fresh_min):
            return []
        fair = settled_by(kind, rng, obs, p["margin"])
        if not self.passes("settled by observation", fair, "the day's readings decide the market", fair is not None):
            return []
        side = side_for(fair, *got, p["edge"])
        if not self.passes("edge", {"fair": fair, "bid": got[0], "ask": got[1]}, p["edge"], side is not None):
            return []
        return self.order(row, side if q else None)


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


def span(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days)]


def months(done: set, today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """(start, end) of every whole calendar month before today's whose every settle date is in `done`, oldest
    first; end is exclusive."""
    out, end = [], today.replace(day=1)
    first = min(done, default=end)
    while end > first:
        start = (end - dt.timedelta(days=1)).replace(day=1)
        if all(d in done for d in span(start, end)):
            out.append((start, end))
        end = start
    return out[::-1]


def recorded_dates(fs, cfg: dict, venue: str) -> set:
    """The days the recorder wrote quotes for the venue: a sportsbook's history is its own recording."""
    sel = pafs.FileSelector(f"{cfg['recorder']['bucket']}/{cfg['recorder']['prefix']}/venue={venue}",
                            allow_not_found=True)
    return {dt.date.fromisoformat(i.base_name.removeprefix("date=")) for i in fs.get_file_info(sel)
            if i.type == pafs.FileType.Directory and i.base_name.startswith("date=")}


def done_dates(conn, venue: str) -> set:
    return {dt.date.fromisoformat(u) for (u,) in conn.execute(
        "SELECT unit FROM market_backfill WHERE venue = %s AND job = 'bars' AND done", (venue,))}


def trained(params: dict, train: tuple, start: dt.date) -> Calibration:
    """A calibration variant fitted on its training book, which must end before the window starts."""
    book, markets, resolutions = train
    before = _midnight(start)
    if len(book) and pd.to_datetime(book["ts"], utc=True).max() >= pd.Timestamp(before):
        raise Lookahead(f"calibration's training book reaches into the window that starts {start}")
    return Calibration(params, fit(book, markets, resolutions, before, params), before)


class Loader:
    """Books for one window at a time, so a run holds at most one window's bars and one training span."""

    def __init__(self, conn, fs, venue: str, cfg: dict):
        self.conn, self.fs, self.venue, self.cfg, self.cache = conn, fs, venue, cfg, {}
        self.sportsbook = cfg["venues"][venue].get("kind") == "sportsbook"
        self.known, self.fairs, self._meetings = None, {}, None

    def book(self, start: dt.date, end: dt.date) -> pd.DataFrame:
        """The window's book: a sportsbook's line book (line_book) of the totals markets that start in it, from the
        recorder's quotes up to LEAD before; any other venue's bars."""
        if not self.sportsbook:
            return self.bars(start, end)
        c = self.cfg
        ids = [r[0] for r in self.conn.execute(
            "SELECT market_id FROM markets WHERE venue = %s AND market_type = 'totals' AND close_time >= %s "
            "AND close_time < %s", (self.venue, _midnight(start), _midnight(end)))]
        q = backtest.read_quotes(self.fs, f"{c['recorder']['bucket']}/{c['recorder']['prefix']}", self.venue,
                                 start - LEAD, end)
        markets, res = backtest.load_markets(self.conn, self.venue, ids)
        book, lm, lr, self.fairs = line_book(q, markets, res, c["backtest"]["sportsbook_depth"])
        self.known = (lm, lr)
        return book

    def meetings(self) -> Meetings:
        """Every resolved totals market of the venue; Meetings shows a strategy only those settled before its row."""
        if self._meetings is None:
            self._meetings = meetings_of(self.conn, self.venue)
        return self._meetings

    def bars(self, start: dt.date, end: dt.date) -> pd.DataFrame:
        c = self.cfg
        return backtest.load_bars(self.fs, f"{c['recorder']['bucket']}/{c['history']['prefix']}/bars", self.venue,
                                  start, end, c["backtest"]["bar_volume_share"])

    def window(self) -> None:
        self.cache, self.known, self.fairs = {}, None, {}

    def train(self, start: dt.date, days: int) -> tuple:
        key = ("train", days, start)
        if key not in self.cache:
            tb = self.bars(start - dt.timedelta(days=days), start)
            self.cache[key] = (tb, *backtest.load_markets(self.conn, self.venue, tb["market_id"].unique()))
        return self.cache[key]

    def observations(self, start: dt.date, end: dt.date) -> Forecasts | None:
        """The window's station observations, or None unless every day of it has a reading."""
        key = ("observations", start)
        if key not in self.cache:
            covered = inputs.days(self.conn, "nws_obs")
            self.cache[key] = Forecasts(inputs.readings(self.conn, "nws_obs", _midnight(start) - LEAD, _midnight(end))) \
                if all(d in covered for d in span(start, end)) else None
        return self.cache[key]

    def forecasts(self, start: dt.date, end: dt.date) -> Forecasts | None:
        """The window's NWS forecasts, from LEAD before it, or None unless every day of it has a reading."""
        key = ("forecasts", start)
        if key not in self.cache:
            covered = inputs.days(self.conn, "nws_high")
            self.cache[key] = Forecasts(inputs.readings(self.conn, "nws_high", _midnight(start) - LEAD, _midnight(end))) \
                if all(d in covered for d in span(start, end)) else None
        return self.cache[key]

    def ref(self, start: dt.date, end: dt.date, ref_venue: str, source: str) -> tuple:
        key = ("ref", ref_venue, source)
        if key not in self.cache:
            links = load_links(self.conn, self.venue, ref_venue, source)
            c = self.cfg
            rq = backtest.read_quotes(self.fs, f"{c['recorder']['bucket']}/{c['recorder']['prefix']}", ref_venue,
                                      start - dt.timedelta(days=1), end)
            rq = rq[rq["market_id"].isin({r[0] for r in links.values()})]
            rm, _ = backtest.load_markets(self.conn, ref_venue, rq["market_id"].unique())
            self.cache[key] = (links, RefBook(rq, rm))
        return self.cache[key]


def strategy(family: str, params: dict, loader: Loader, start: dt.date, end: dt.date, done: set):
    """The variant ready for one window, or None when its inputs are not all backfilled."""
    if family == "longshot":
        return Longshot(params)
    if family == "tt_totals":
        return TTTotals(params, loader.meetings(), loader.fairs)
    if family == "calibration":
        days = params["train_days"]
        if not all(d in done for d in span(start - dt.timedelta(days=days), start)):
            return None
        return trained(params, loader.train(start, days), start)
    if family == "weather":
        f = loader.forecasts(start, end)
        return None if f is None else Weather(params, f, inputs.load())
    if family == "observed":
        o = loader.observations(start, end)
        return None if o is None else Observed(params, o, inputs.load())
    return Gap(params, *loader.ref(start, end, params["ref_venue"], params["source"]))


def day(t: dt.datetime) -> dt.date:
    """A window's first day, whatever zone the connection hands its start in."""
    return t.astimezone(UTC).date()


def stored(conn, venue: str) -> set:
    return {(f, v, day(s)) for f, v, s in conn.execute(
        "SELECT family, variant, start_at FROM market_backtests WHERE venue = %s", (venue,))}


def walk(conn, loader: Loader, names, windows, done: set, cfg: dict, rerun: bool = False, only: set | None = None,
         stop: float | None = None) -> int:
    """Run every variant of every family in `names` on each window it has no row for (every window with rerun),
    oldest first; `only` limits it to those (family, variant). Starts no window once past `stop` (monotonic)."""
    have, ran = set() if rerun else stored(conn, loader.venue), 0
    for start, end in windows:
        todo = [(f, p) for f in names for p in variants(cfg["families"][f])
                if (f, backtest.variant_of(p), start) not in have and (only is None or (f, backtest.variant_of(p)) in only)]
        if not todo:
            continue
        if stop is not None and time.monotonic() > stop:
            print(f"families: budget spent, {start} and later wait for the next run", flush=True)
            break
        loader.window()
        book = loader.book(start, end)
        print(f"families: {loader.venue} {start} to {end}: {len(book)} rows, {book['market_id'].nunique()} markets",
              flush=True)
        for family, params in todo:
            s = strategy(family, params, loader, start, end, done)
            if s is None:
                continue
            out = backtest.run(conn, s, loader.venue, book, _midnight(start), _midnight(end), cfg, known=loader.known)
            ran += 1
            print(f"families: {family} {backtest.variant_of(params)[:8]} {start}: {out}", flush=True)
        del book
    return ran


def daily_of(rows: list[dict]) -> pd.Series:
    """The windows' daily returns as one series by day."""
    parts = [pd.Series(r["daily"], index=pd.date_range(day(r["start_at"]), periods=len(r["daily"]), freq="D"))
             for r in rows if len(r["daily"])]
    return pd.concat(parts).groupby(level=0).sum().sort_index() if parts else pd.Series(dtype=float)


def verdict(windows: list[dict], lockbox: list[dict] | None, pbo: float | None, trials: int, c: dict,
            seed: int) -> dict:
    """The readiness gates of one variant over its test windows (module docstring). `lockbox` is its lockbox
    windows once read, or None before every other gate has passed."""
    windows = sorted(windows, key=lambda r: r["start_at"])
    returns = [x for r in windows for x in r["returns"]]
    ev = confidence.interval(returns, seed, c, [e for r in windows for e in r["events"]])
    daily = daily_of(windows)
    sd = daily.std(ddof=1) if len(daily) > 2 else 0.0
    sr = float(daily.mean() / sd) if sd and math.isfinite(sd) else None
    dsr = None if sr is None else metrics.deflated_sharpe(sr, len(daily), float(daily.skew()),
                                                          float(daily.kurt()) + 3.0, trials)
    years = len(daily) / metrics.YEAR
    need = metrics.min_backtest_years(None if sr is None else sr * math.sqrt(metrics.YEAR), trials)
    months = [float(np.mean(r["returns"])) for r in windows if len(r["returns"])]
    share = sum(m > 0 for m in months) / len(months) if months else None
    g = [
        check("out-of-sample trades", ev["trades"], f">= {c['min_oos_trades']}", ev["trades"] >= c["min_oos_trades"]),
        check("expectancy interval low, by event", ev["lo"], f"> 0 at {c['expectancy_level']:.0%}",
              ev["lo"] is not None and ev["lo"] > 0),
        check(f"deflated Sharpe, {trials} trials", dsr, f">= {c['min_dsr']}", dsr is not None and dsr >= c["min_dsr"]),
        check("overfit probability", pbo, f"<= {c['max_pbo']}", pbo is not None and pbo <= c["max_pbo"]),
        check("minimum backtest length", None if need is None else f"{years:.1f} y of {need:.1f} y",
              f"out-of-sample years >= {c['min_btl_ratio']:g} x 2 ln({trials}) / SR^2",
              need is not None and years >= c["min_btl_ratio"] * need),
        check("months positive", None if share is None else f"{sum(m > 0 for m in months)} of {len(months)}",
              f">= {c['min_folds_positive']:.0%} of >= {c['min_folds']}",
              len(months) >= c["min_folds"] and share >= c["min_folds_positive"]),
    ]
    held = [x for r in lockbox or [] for x in r["returns"]]
    g.append(check("lockbox", "not read until the rest pass" if lockbox is None else
                   (float(np.mean(held)) if held else "no trades"), "> 0, read once", bool(held) and bool(np.mean(held) > 0)))
    return {"windows": len(windows), "trades": ev["trades"], "expectancy": ev["mean"], "expectancy_lo": ev["lo"],
            "expectancy_hi": ev["hi"], "dsr": dsr, "pbo": pbo, "trials": trials, "gates": g,
            "ready": all(x["pass"] for x in g), "passed": sum(x["pass"] for x in g)}


COLS = ("family", "variant", "params", "start_at", "returns", "events", "daily")
ROWS = f"SELECT {', '.join(COLS)} FROM market_backtests WHERE venue = %s"


def gate(conn, loader: Loader, names, test, lockbox, done: set, cfg: dict, ccfg: dict) -> dict:
    """Write each current variant's readiness row (module docstring); replays the lockbox for those that pass
    every other gate. Returns the passing count per family."""
    venue, starts = loader.venue, {s for s, _ in test}
    rows = defaultdict(list)
    for r in conn.execute(ROWS, (venue,)).fetchall():
        r = dict(zip(COLS, r))
        if day(r["start_at"]) in starts:
            rows[(r["family"], r["variant"])].append(r)
    blocks = cfg["families"]["pbo_blocks"]
    out = {}
    for family in names:
        grid = {backtest.variant_of(p): p for p in variants(cfg["families"][family])}
        panel = metrics.pbo_panel({v: daily_of(rows[(family, v)]) for v in grid}, blocks)
        pbo = None if panel is None else panel["pbo"]

        def judge(v: str, lock):
            return verdict(rows[(family, v)], lock, pbo, backtest.trial_count(conn, cfg), ccfg,
                           metrics.seed_of(family, venue, v, "readiness"))

        result = {v: judge(v, None) for v in grid}
        near = {v for v, r in result.items() if all(x["pass"] for x in r["gates"][:-1])}
        if near and lockbox:
            walk(conn, loader, [family], lockbox, done, cfg, only={(family, v) for v in near})
            held, keep = defaultdict(list), {s for s, _ in lockbox}
            for r in conn.execute(ROWS + " AND family = %s", (venue, family)).fetchall():
                r = dict(zip(COLS, r))
                if day(r["start_at"]) in keep:
                    held[r["variant"]].append(r)
            result.update({v: judge(v, held[v]) for v in near})
        readiness.store(conn, readiness.EVENTS, [
            {**{k: r[k] for k in ("windows", "trades", "expectancy", "expectancy_lo", "expectancy_hi", "dsr", "pbo",
                                  "trials", "ready", "passed", "gates")},
             "venue": venue, "strategy": f"{family}/{v}", "family": family, "variant": v, "params": grid[v]}
            for v, r in result.items()], venue=venue, family=family)
        out[family] = sum(r["ready"] for r in result.values())
    return out


def main(argv: list[str] | None = None, fs=None, today: dt.date | None = None) -> int:
    ap = argparse.ArgumentParser(prog="markets.families")
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--family", nargs="*", choices=COMMANDS + LINES, help="default: every family the venue's kind runs")
    ap.add_argument("--rerun", action="store_true", help="run every window again, stored or not")
    a = ap.parse_args(argv)
    cfg, ccfg = load_config(), suggest.load_config()["confidence"]
    fam = cfg["families"]
    if set(categories()) != {name for name, grid in fam.items() if isinstance(grid, dict)}:
        raise ValueError("every market family needs a category")
    sportsbook = cfg["venues"][a.venue].get("kind") == "sportsbook"
    fits = LINES if sportsbook else COMMANDS
    a.family = a.family or list(fits)
    if set(a.family) - set(fits):
        ap.error(f"{a.venue} runs {', '.join(fits)}: a sportsbook runs line families, an exchange the others")
    today = today or dt.datetime.now(UTC).date()
    stop = time.monotonic() + fam["budget_min"] * 60
    fs = fs or s3_from_env()
    with connect(autocommit=True) as conn:
        done = recorded_dates(fs, cfg, a.venue) if sportsbook else done_dates(conn, a.venue)
        windows = months(done, today)
        cut = max(len(windows) - fam["lockbox_months"], 0)
        test, lockbox = windows[:cut], windows[cut:]
        print(f"families: {a.venue}: {len(test)} test windows, lockbox {[str(s) for s, _ in lockbox]}", flush=True)
        loader = Loader(conn, fs, a.venue, cfg)
        ran = walk(conn, loader, a.family, test, done, cfg, a.rerun, stop=stop)
        passed = gate(conn, loader, a.family, test, lockbox, done, cfg, ccfg)
    print(json.dumps({"venue": a.venue, "windows": len(test), "lockbox": len(lockbox), "ran": ran, "ready": passed}),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
