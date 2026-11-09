"""Event-contract backtests: replay a venue's recorded books through a strategy, fill its orders the way a taker is
filled, settle each fill at resolution, and score the result with the crypto lab's own statistics.

The replay (replay, settle):
- A book is one row per market and time: bid, ask, and the contracts each side can take, depth_yes at the ask and
  depth_no at the bid. load_quotes builds one from the recorder's change-only quotes; load_bars builds one from
  the history backfill's bars, which carry no sizes, so there each side's depth is `bar_volume_share` of the
  contracts the period traded.
- The strategy sees the rows in time order, each with its market's metadata and never its resolution, and
  returns orders. It is handed nothing from later than the row, so it cannot read the future.
- An order fills against the book as it stood `latency_s` later (the market's last row at or before then): YES at
  the ask, NO at one minus the bid, so it always crosses the spread. It takes whole contracts, at most its stake
  and its limit price allow, and at most `depth_share` of that side's depth less what earlier orders took from the
  same row. The rest is cancelled. Nothing fills on a market with no book yet, or one closed or settled by then.
- A fill is held to resolution. YES pays the resolution's value, NO pays one minus it, and a void market refunds
  the price but not the fee. A strategy exits early by buying the other side: a YES and a NO pay exactly one.
- A strategy with a `cutoff` (an LLM forecaster's training cutoff) is refused outright on a book that starts
  before it: its forecasts of those events are lookahead.
- Fees are lab/markets/fees.py, from `[backtest.fees.<venue>]`.

The result (evaluate) is a trade table in the shape metrics.window_metrics reads, one row per fill: open_date the
fill, close_date the settlement, open_rate the price, close_rate the payout per contract, profit_abs net of every
fee, profit_ratio that over the cost with fees. Daily returns chain on `wallet` (metrics.daily_returns), the
interval on the per-trade return is confidence.interval, and the score over every stored backtest is
confidence.scores with suggest.toml [confidence], so an event contract clears the same bar a strategy does.

run() registers the trial before it reads any result, in the crypto lab's `trials` register (strategy = the
family, exchange = the venue, variant = a hash of the params), stores one market_backtests row per family, venue
and variant (migration 022), and rescores them all. The deflated Sharpe counts every trial on any markets venue.
"""

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable, Protocol

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb
from pyarrow import fs as pafs

from lab import confidence, metrics, suggest
from lab.grid import variant_of
from lab.markets import fees
from lab.markets.recorder import load_config
from lab.markets.venue import Resolution
from lab.util import finite

BOOK = ["market_id", "ts", "bid", "ask", "depth_yes", "depth_no"]
SIDES = ("yes", "no")


class Lookahead(ValueError):
    """A backtest that would let a strategy use what it could not have known."""


@dataclass(frozen=True, slots=True)
class Market:
    market_id: str
    event_id: str | None
    series_id: str | None
    title: str
    subtitle: str | None
    market_type: str
    open_time: dt.datetime | None
    close_time: dt.datetime | None


@dataclass(frozen=True, slots=True)
class Order:
    market_id: str
    side: str  # yes or no
    stake: float  # dollars of contracts, before fees
    limit: float | None = None  # the most it pays per contract


class Strategy(Protocol):
    name: str  # the family, as the trial register names it
    params: dict  # the variant
    cutoff: dt.datetime | None  # forward only: refused on any book that starts before this

    def on_book(self, row, market: Market) -> Iterable[Order]:
        """One book row (market_id, ts, bid, ask, depth_yes, depth_no) and its market; the orders to send."""
        ...


def _days(start: dt.date, end: dt.date) -> list[str]:
    return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days)]


def _book(df: pd.DataFrame) -> pd.DataFrame:
    df = df.assign(ts=pd.to_datetime(df["ts"], utc=True).astype("datetime64[ns, UTC]"))
    return df[BOOK].sort_values(["ts", "market_id"], kind="stable").reset_index(drop=True)


def _empty() -> pd.DataFrame:
    return _book(pd.DataFrame({c: pd.Series(dtype="datetime64[ns, UTC]" if c == "ts" else "object" if c == "market_id"
                                            else float) for c in BOOK}))


def read_quotes(fs: pafs.FileSystem, root: str, venue: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Every outcome's quotes from the recorder's objects (<root>/venue=<v>/date=<d>/*.parquet) for the days start
    to end, end exclusive, as written: market_id, outcome, ts, bid, ask, bid_size, ask_size."""
    cols = ["market_id", "outcome", "ts", "bid", "ask", "bid_size", "ask_size"]
    parts = []
    for d in _days(start, end):
        sel = pafs.FileSelector(f"{root}/venue={venue}/date={d}", allow_not_found=True)
        for info in sorted(fs.get_file_info(sel), key=lambda i: i.path):
            if info.type == pafs.FileType.File and info.path.endswith(".parquet"):
                parts.append(pq.read_table(info.path, filesystem=fs, columns=cols).to_pandas())
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)


def load_quotes(fs: pafs.FileSystem, root: str, venue: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """The recorder's yes quotes for the days start to end, end exclusive, as a book. Recording is change-only, so
    a market first appears at its first change in the window. A run of equal quotes, which a recorder retry
    writes, keeps its first row."""
    q = read_quotes(fs, root, venue, start, end)
    if q.empty:
        return _empty()
    q = q[q["outcome"] == "yes"].sort_values(["market_id", "ts"], kind="stable")
    book = ["bid", "ask", "bid_size", "ask_size"]
    same = (q[book].fillna(-1) == q.groupby("market_id")[book].shift().fillna(-1)).all(axis=1)
    q = q[~same | (q["market_id"] != q["market_id"].shift())]
    return _book(q.assign(depth_yes=q["ask_size"], depth_no=q["bid_size"]))


def load_bars(fs: pafs.FileSystem, root: str, venue: str, start: dt.date, end: dt.date, share: float) -> pd.DataFrame:
    """The backfill's bars (<root>/venue=<v>/date=<d>.parquet, d the settle date) of every market settled start to
    end, end exclusive. A bar has no sizes, so each side may take `share` of the contracts the period traded."""
    parts = []
    for d in _days(start, end):
        key = f"{root}/venue={venue}/date={d}.parquet"
        if fs.get_file_info(key).type == pafs.FileType.File:
            parts.append(pq.read_table(key, filesystem=fs, columns=["market_id", "ts", "bid", "ask", "volume"]).to_pandas())
    if not parts:
        return _empty()
    b = pd.concat(parts, ignore_index=True)
    depth = b["volume"].astype(float) * share
    return _book(b.assign(depth_yes=depth, depth_no=depth))


def load_markets(conn, venue: str, ids: Iterable[str]) -> tuple[dict, dict]:
    """The markets behind a book and the resolutions of those that have one."""
    ids = list(ids)
    markets = {r[0]: Market(*r) for r in conn.execute(
        "SELECT market_id, event_id, series_id, title, subtitle, market_type, open_time, close_time FROM markets "
        "WHERE venue = %s AND market_id = ANY(%s)", (venue, ids))}
    res = {r[0]: Resolution(*r) for r in conn.execute(
        "SELECT market_id, result, value, settled_at, volume FROM resolutions WHERE venue = %s AND market_id = ANY(%s)",
        (venue, ids))}
    return markets, res


def take(o: Order, bid: float, ask: float, depth_yes: float, depth_no: float, taken: float,
         share: float) -> tuple[int, float] | None:
    """The whole contracts and price a taker order gets from one book row (module docstring), `taken` contracts of
    its side already gone to earlier orders; None when it gets nothing. The paper league fills through this too."""
    if o.side not in SIDES:
        raise ValueError(f"order side {o.side!r}")
    price = ask if o.side == "yes" else 1.0 - bid
    if not 0 < price < 1 or (o.limit is not None and price > o.limit):
        return None
    room = (depth_yes if o.side == "yes" else depth_no) * share - taken
    n = min(math.floor(o.stake / price + 1e-9), math.floor(room)) if np.isfinite(room) else 0
    return (n, float(price)) if n >= 1 else None


def payout(side: str, price: float, res: Resolution) -> float | None:
    """What one contract bought at `price` pays at resolution (module docstring); None for an unknown result."""
    if res.result == "void":
        return price
    v = res.value if res.value is not None else {"yes": 1.0, "no": 0.0}.get(res.result)
    if v is None:
        return None
    return v if side == "yes" else 1.0 - v


def replay(book: pd.DataFrame, markets: dict, resolutions: dict, strategy: Strategy, bcfg: dict,
           fee_cfg: dict | None) -> tuple[pd.DataFrame, int]:
    """Every fill the strategy's orders get from the book, and how many orders it sent."""
    cutoff = getattr(strategy, "cutoff", None)
    if cutoff is not None and len(book) and book["ts"].min() < pd.Timestamp(cutoff):
        raise Lookahead(f"{strategy.name} is forward only from {cutoff}; this book starts {book['ts'].min()}")
    book = _book(book)
    ns = book["ts"].to_numpy(dtype="datetime64[ns]").astype(np.int64)
    rows = {m: idx.to_numpy() for m, idx in book.groupby("market_id", sort=False).groups.items()}
    bid, ask = book["bid"].to_numpy(dtype=float), book["ask"].to_numpy(dtype=float)
    depth = {"yes": book["depth_yes"].to_numpy(dtype=float), "no": book["depth_no"].to_numpy(dtype=float)}
    latency, share = int(bcfg["latency_s"] * 1e9), bcfg["depth_share"]
    taken: dict = {}
    fills, sent = [], 0

    def fill(o: Order, t: int) -> dict | None:
        idx, at = rows.get(o.market_id), pd.Timestamp(t, tz="UTC")
        if idx is None:
            return None
        j = int(np.searchsorted(ns[idx], t, side="right")) - 1
        m, res = markets.get(o.market_id), resolutions.get(o.market_id)
        if j < 0 or (m and m.close_time and at >= m.close_time) or (res and at >= res.settled_at):
            return None
        r = idx[j]
        got = take(o, bid[r], ask[r], depth["yes"][r], depth["no"][r], taken.get((r, o.side), 0), share)
        if got is None:
            return None
        n, price = got
        taken[(r, o.side)] = taken.get((r, o.side), 0) + n
        return {"market_id": o.market_id, "side": o.side, "open_date": at, "contracts": n, "price": price,
                "fee": fees.entry(fee_cfg, n, price)}

    for i, row in enumerate(book.itertuples(index=False)):
        m = markets.get(row.market_id)
        if m is None:
            continue
        for o in strategy.on_book(row, m) or ():
            sent += 1
            f = fill(o, int(ns[i]) + latency)
            if f:
                fills.append(f)
    return pd.DataFrame(fills, columns=["market_id", "side", "open_date", "contracts", "price", "fee"]), sent


def settle(fills: pd.DataFrame, resolutions: dict, fee_cfg: dict | None) -> pd.DataFrame:
    """The fills of resolved markets as trades (module docstring), in settlement order. Unresolved fills drop."""
    out = []
    for f in fills.itertuples(index=False):
        res = resolutions.get(f.market_id)
        if res is None or (pay := payout(f.side, f.price, res)) is None:
            continue
        cost = f.contracts * f.price + f.fee
        profit = f.contracts * pay - cost - fees.settlement(fee_cfg, f.contracts * f.price, f.contracts * pay)
        out.append({"market_id": f.market_id, "side": f.side, "contracts": f.contracts, "open_date": f.open_date,
                    "close_date": pd.Timestamp(res.settled_at), "open_rate": f.price, "close_rate": float(pay),
                    "fee": f.fee, "profit_abs": profit, "profit_ratio": profit / cost, "min_rate": np.nan})
    cols = ["market_id", "side", "contracts", "open_date", "close_date", "open_rate", "close_rate", "fee", "profit_abs",
            "profit_ratio", "min_rate"]
    t = pd.DataFrame(out, columns=cols)
    return t.sort_values(["close_date", "open_date"], kind="stable").reset_index(drop=True)


def evaluate(trades: pd.DataFrame, start: dt.datetime, end: dt.datetime, wallet: float, seed: int,
             ccfg: dict, trials: int) -> dict:
    """window_metrics over the replay's days (to the last settlement, if later than end), the per-trade interval,
    and the deflated Sharpe at `trials`."""
    until = max(pd.Timestamp(end), trades["close_date"].max() + pd.Timedelta(days=1)) if len(trades) else end
    daily = metrics.daily_returns(trades, start, until, wallet)
    w = metrics.window_metrics(daily, trades, seed)
    ev = confidence.interval(trades["profit_ratio"], seed, ccfg)
    dsr = metrics.deflated_sharpe(w["_sr_daily"], w["days"], w["_skew"], w["_kurt"], trials)
    m = {k: v.isoformat() if isinstance(v, dt.date) else finite(v)
         for k, v in w.items() if not k.startswith("_")}
    return {"metrics": m, "interval": ev, "dsr": dsr}


def category_of(series_id: str | None, categories: dict) -> str:
    """The contract category of a series by the prefixes in markets.toml [categories]; "other" when none matches."""
    s = (series_id or "").lower()
    return next((c for c, prefixes in categories.items() if any(s.startswith(p.lower()) for p in prefixes)), "other")


def by_category(trades: pd.DataFrame, markets: dict, categories: dict, seed: int, ccfg: dict) -> list[tuple]:
    """(category, trades, mean, lo, hi) of the net return per trade, one row per category with a trade."""
    if not len(trades):
        return []
    cat = trades["market_id"].map(lambda m: category_of(markets[m].series_id if m in markets else None, categories))
    out = []
    for c in sorted(cat.unique()):
        e = confidence.interval(trades.loc[cat == c, "profit_ratio"], metrics.seed_of(seed, c), ccfg)
        out.append((c, e["trades"], e["mean"], e["lo"], e["hi"]))
    return out


def rescore(conn, ccfg: dict) -> int:
    """confidence.scores over every stored backtest; a row outside the population gets no score."""
    ev = {(f, v, var): confidence.interval(r, metrics.seed_of(f, v, var, "confidence"), ccfg)
          for f, v, var, r in conn.execute("SELECT family, venue, variant, returns FROM market_backtests")}
    s = confidence.scores(ev, ccfg)
    with conn.cursor() as cur:
        cur.executemany("UPDATE market_backtests SET score = %s WHERE family = %s AND venue = %s AND variant = %s",
                        [(s.get(k), *k) for k in ev])
    return len(s)


def run(conn, strategy: Strategy, venue: str, book: pd.DataFrame, start: dt.datetime, end: dt.datetime,
        cfg: dict | None = None, ccfg: dict | None = None) -> dict:
    """Register the trial, replay, settle, evaluate, store and rescore. start and end bound the book's days."""
    cfg = cfg or load_config()
    ccfg = ccfg or suggest.load_config()["confidence"]
    bcfg, variant = cfg["backtest"], variant_of(strategy.params)
    conn.execute("INSERT INTO trials (strategy, exchange, variant) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                 (strategy.name, venue, variant))
    trials = conn.execute("SELECT count(*) FROM trials WHERE exchange = ANY(%s)", (list(cfg["venues"]),)).fetchone()[0]
    markets, res = load_markets(conn, venue, book["market_id"].unique().tolist())
    fee_cfg = bcfg.get("fees", {}).get(venue)
    fills, sent = replay(book, markets, res, strategy, bcfg, fee_cfg)
    trades = settle(fills, res, fee_cfg)
    e = evaluate(trades, start, end, bcfg["wallet"], metrics.seed_of(strategy.name, venue, variant, "confidence"),
                 ccfg, trials)
    ev = e["interval"]
    conn.execute(
        """INSERT INTO market_backtests (family, venue, variant, params, start_at, end_at, orders, fills, trades,
               open_fills, metrics, returns, expectancy, expectancy_lo, expectancy_hi, dsr, trials, computed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (family, venue, variant) DO UPDATE SET params = EXCLUDED.params, start_at = EXCLUDED.start_at,
               end_at = EXCLUDED.end_at, orders = EXCLUDED.orders, fills = EXCLUDED.fills, trades = EXCLUDED.trades,
               open_fills = EXCLUDED.open_fills, metrics = EXCLUDED.metrics, returns = EXCLUDED.returns,
               expectancy = EXCLUDED.expectancy, expectancy_lo = EXCLUDED.expectancy_lo,
               expectancy_hi = EXCLUDED.expectancy_hi, dsr = EXCLUDED.dsr, trials = EXCLUDED.trials,
               score = NULL, computed_at = now()""",
        (strategy.name, venue, variant, Jsonb(strategy.params), start, end, sent, len(fills), len(trades),
         len(fills) - len(trades), Jsonb(e["metrics"]),
         ev["returns"].tolist(), ev["mean"], ev["lo"], ev["hi"], e["dsr"], trials))
    conn.execute("DELETE FROM market_category_results WHERE family = %s AND venue = %s AND variant = %s",
                 (strategy.name, venue, variant))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO market_category_results (family, venue, variant, category, trades, mean, lo, hi) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(strategy.name, venue, variant, *r) for r in by_category(
                            trades, markets, cfg.get("categories", {}), metrics.seed_of(strategy.name, venue, variant), ccfg)])
    scored = rescore(conn, ccfg)
    return {"orders": sent, "fills": len(fills), "trades": len(trades), "expectancy": ev["mean"], "lo": ev["lo"],
            "hi": ev["hi"], "dsr": e["dsr"], "trials": trials, "scored": scored}
