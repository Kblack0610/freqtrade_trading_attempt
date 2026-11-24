#!/usr/bin/env python3
"""By hand: Carver's carry strategies (AFTS 10, 11, 15, 16, 20) measured from recorded funding, as a markdown report.

python lab/futures/research/carry.py <userdir>, inside the image with DATABASE_URL set. Research only: nothing here trades.
Funding comes from funding_rates (lab/futures/funding.py), kucoin's series, the only one that starts before the lockbox
(okx's starts 2026-08-03). Spot candles are kucoin's daily candles, downloaded into the userdir once. The window
runs from the first funding day to the lockbox start in lab/crypto/suggest.toml, so the lockbox months stay unseen.
Every rule measured here is a row in the trial register (migration 059). See docs/carry.md.

Timing: a settlement counts on the UTC day it settles, a day's spot return is that day's close over the last, and
every position held on day d is decided from funding and closes through day d - 1.
"""

import json
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

from lab import forecasts, metrics
from lab.crypto import suggest
from lab.futures.research.bust import freqtrade

REPO = Path(__file__).resolve().parents[3]
VENUE = "kucoin"
FEE = 0.001  # Per leg per side: kucoin's 0.1% spot taker; the perp leg's taker is lower, the rest stands for slippage.
CAPITAL = 2.0  # A harvest holds the spot and posts the same again as perp margin (1x), so capital is twice the notional.
SPANS = (5, 20, 60, 120)  # Carver's carry smoothing spans, in days.
CANDLES_FROM = "20230101"  # A year before the first funding, so the slowest EWMAC has warmed up.
# The AFTS 9 mix's settings, from the forecast family's TrendMix (CarverTrendMix), so the two cannot drift.
TREND = tomllib.loads((REPO / "user_data" / "strategies" / "forecast" / "family.toml").read_text())[
    "strategies"]["TrendMix"]["args"]
TREND_WEIGHT = 0.6  # AFTS 11 and 16: Carver's 60/40 trend to carry.
SEED = 346
# Each rule's row in the trial register (migration 059). The forecast tests count too; a robustness cut of one
# (without KCS and ZEC) is the same signal, and buying every coin is the baseline, so neither adds a trial.
TRIALS = {
    "harvest, always": ("CarryHarvest", "always"),
    "harvest, smoothed funding > 0": ("CarryHarvest", "smoothed"),
    "harvest, last day's funding > 0": ("CarryHarvest", "last-day"),
    "harvest, top third of smoothed funding": ("CarryHarvest", "top-third"),
    "spot, trend > 0": ("CarrySpot", "trend"),
    "spot, trend > 0 and smoothed funding <= median": ("CarrySpot", "trend-low-funding"),
    "spot, lowest third of smoothed funding": ("CarrySpot", "low-funding-third"),
    "60/40 trend and harvest": ("CarryMix", "60-40"),
    "smoothed funding": ("FundingForecast", "level"),
    "smoothed funding against its own history": ("FundingForecast", "own-history"),
}
BASELINES = ("spot, every coin",)


def lockbox() -> pd.Timestamp:
    return pd.Timestamp(suggest.load_config()["lockbox"]["start"])


def daily_funding(rates: pd.DataFrame) -> pd.DataFrame:
    """Settlements summed by UTC day, one column per coin; a day with no settlement is NaN."""
    day = pd.to_datetime(rates["ts"], utc=True).dt.tz_localize(None).dt.normalize()
    coin = rates["pair"].str.split("/").str[0]
    return rates.assign(day=day, coin=coin).pivot_table(index="day", columns="coin", values="rate", aggfunc="sum")


def smoothed(funding: pd.DataFrame) -> pd.DataFrame:
    """AFTS 15: the mean over Carver's spans of the EWMA of daily funding, as of each day's end. Shift it a day to use it."""
    return sum(funding.ewm(span=s, min_periods=s).mean() for s in SPANS) / len(SPANS)


def costs(weights: pd.DataFrame, legs: int) -> pd.Series:
    """What changing the weights costs each day, as a share of the sleeve: every leg pays FEE on what it trades."""
    return weights.fillna(0.0).diff().abs().fillna(weights.fillna(0.0).abs()).sum(axis=1) * legs * FEE


def weights(hold: pd.DataFrame, listed: pd.DataFrame) -> pd.DataFrame:
    """One equal slot per listed coin, filled when the rule holds it and in cash when not, so a coin joining or
    leaving does not resize the others."""
    n = listed.sum(axis=1)
    return (hold & listed).astype(float).div(n.where(n > 0), axis=0).fillna(0.0)


def harvest(funding: pd.DataFrame, hold: pd.DataFrame, listed: pd.DataFrame) -> pd.Series:
    """Daily return on capital of long spot plus short perp in each held slot. A short receives a positive rate.
    Basis moves are left out: the perp is assumed to track spot (docs/carry.md, Limits)."""
    w = weights(hold, listed)
    cost = costs(w, 2) / CAPITAL
    out = (w * funding.fillna(0.0)).sum(axis=1) / CAPITAL - cost
    out.attrs["cost"] = float(cost.mean() * metrics.YEAR)
    return out


def spot_sleeve(returns: pd.DataFrame, hold: pd.DataFrame, listed: pd.DataFrame) -> pd.Series:
    """Daily return of long spot in each held slot, cash otherwise."""
    w = weights(hold, listed)
    cost = costs(w, 1)
    out = (w * returns.fillna(0.0)).sum(axis=1) - cost
    out.attrs["cost"] = float(cost.mean() * metrics.YEAR)
    return out


def trend(close: pd.DataFrame) -> pd.DataFrame:
    """The AFTS 9 EWMAC mix per coin, as of each day's close."""
    return close.apply(lambda c: forecasts.ewmac_mix(c.dropna(), TREND["fasts"], TREND["fdm"]).reindex(c.index))


def lowest_third(x: pd.DataFrame) -> pd.DataFrame:
    """True for the coins in each day's lowest third of x; all False on a day with under three values."""
    rank = x.rank(axis=1, pct=True)
    return (rank <= 1 / 3) & (x.notna().sum(axis=1) >= 3).to_numpy()[:, None]


def quintile_returns(signal: pd.DataFrame, forward: pd.DataFrame) -> pd.DataFrame:
    """Mean forward return by each day's cross-sectional quintile of the signal, pooled over days and coins."""
    rank = signal.rank(axis=1, pct=True).stack()
    fwd = forward.stack().reindex(rank.index)
    q = np.ceil(rank * 5).clip(1, 5)
    return pd.DataFrame({"q": q, "fwd": fwd}).dropna().groupby("q")["fwd"].agg(["mean", "count"])


def spread(signal: pd.DataFrame, forward: pd.DataFrame) -> pd.Series:
    """Each day's mean forward return of the lowest-signal fifth of coins minus the highest fifth's."""
    rank = signal.rank(axis=1, pct=True)
    fwd = forward.reindex_like(signal).where(rank.notna())
    return (fwd.where(rank <= 0.2).mean(axis=1) - fwd.where(rank > 0.8).mean(axis=1)).dropna()


def own_history(carry: pd.DataFrame) -> pd.DataFrame:
    """Smoothed funding less its own mean so far, per coin: how crowded a coin is against itself, not the others."""
    return carry - carry.expanding(min_periods=60).mean()


def stats(ret: pd.Series) -> dict:
    r = ret.dropna().to_numpy()
    lo, hi = metrics.sharpe_ci(r, SEED)
    return {"annual": float(r.mean() * metrics.YEAR), "sharpe": metrics.sharpe(r), "lo": lo, "hi": hi,
            "drawdown": metrics.max_drawdown(r), "days": len(r), "cost": ret.attrs.get("cost")}


def fmt(x: float | None, pct: bool = False) -> str:
    return "" if x is None or not np.isfinite(x) else (f"{x:+.1%}" if pct else f"{x:+.2f}")


def row(name: str, s: dict) -> str:
    return (f"| {name} | {fmt(s['annual'], True)} | {fmt(s['sharpe'])} | {fmt(s['lo'])} to {fmt(s['hi'])} "
            f"| {fmt(s['cost'], True)} | {s['drawdown']:.1%} | {s['days']} |")


def study(funding: pd.DataFrame, close: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.Series]:
    """Every rule's daily returns over [start, end). Signals read through the day before."""
    close = close.sort_index()
    ret = close.pct_change(fill_method=None)
    coins = sorted(set(funding.columns) & set(close.columns))
    funding, close, ret = funding[coins], close[coins], ret[coins]
    days = pd.date_range(start, end - pd.Timedelta(days=1), freq="D")
    carry = smoothed(funding).shift(1).reindex(days)
    raw = funding.shift(1).reindex(days)
    up = (trend(close).shift(1) > 0).reindex(days, fill_value=False)
    f, r = funding.reindex(days), ret.reindex(days)
    listed = f.notna() & r.notna()
    out = {
        "harvest, always": harvest(f, listed, listed),
        "harvest, smoothed funding > 0": harvest(f, carry > 0, listed),
        "harvest, last day's funding > 0": harvest(f, raw > 0, listed),
        "harvest, top third of smoothed funding": harvest(f, lowest_third(-carry), listed),
        "spot, every coin": spot_sleeve(r, listed, listed),
        "spot, trend > 0": spot_sleeve(r, up, listed),
        "spot, trend > 0 and smoothed funding <= median": spot_sleeve(r, up & (carry.rank(axis=1, pct=True) <= 0.5), listed),
        "spot, lowest third of smoothed funding": spot_sleeve(r, lowest_third(carry), listed),
    }
    out["60/40 trend and harvest"] = TREND_WEIGHT * out["spot, trend > 0"] + (1 - TREND_WEIGHT) * out["harvest, always"]
    return out


def load_close(user: Path, coins: list[str]) -> pd.DataFrame:
    from freqtrade.data.history import load_pair_history

    conf = user / "carry.json"
    conf.write_text(json.dumps({
        "dry_run": True, "stake_currency": "USDT", "pairlists": [{"method": "StaticPairList"}],
        "exchange": {"name": VENUE, "key": "", "secret": "", "pair_whitelist": [f"{c}/USDT" for c in coins]},
    }))
    if not (user / "data" / VENUE).exists():
        freqtrade("download-data", "--userdir", str(user), "--config", str(conf), "--timeframes", "1d",
                  "--timerange", f"{CANDLES_FROM}-")
    out = {}
    for c in coins:
        df = load_pair_history(f"{c}/USDT", "1d", user / "data" / VENUE)
        if len(df):
            out[c] = df.set_index(df["date"].dt.tz_localize(None).dt.normalize())["close"]
    return pd.DataFrame(out)


def main() -> None:
    from lab.core.db import connect

    user = Path(sys.argv[1]).resolve()
    (user / "data").mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        rates = pd.DataFrame(conn.execute("SELECT pair, ts, rate FROM funding_rates WHERE exchange = %s ORDER BY ts",
                                          (VENUE,)).fetchall(), columns=["pair", "ts", "rate"])
    funding = daily_funding(rates)
    start, end = funding.index.min() + pd.Timedelta(days=1), lockbox()
    close = load_close(user, list(funding.columns))
    print(f"kucoin funding {funding.index.min():%Y-%m-%d} to {funding.index.max():%Y-%m-%d}, "
          f"{funding.shape[1]} coins; window {start:%Y-%m-%d} to {end:%Y-%m-%d} (lockbox start, excluded)\n")

    f = funding[(funding.index >= start) & (funding.index < end)]
    print("| Coin | Funding a year | Days positive | Funding a year, lowest year |\n|---|---|---|---|")
    for c in sorted(f.columns):
        s = f[c].dropna()
        yearly = s.groupby(s.index.year).mean() * metrics.YEAR
        print(f"| {c} | {s.mean() * metrics.YEAR:+.1%} | {(s > 0).mean():.0%} | {yearly.min():+.1%} ({yearly.idxmin()}) |")

    print("\n| Rule | Return a year | Sharpe | Sharpe 90% interval | Fees a year | Max drawdown | Days |\n|---|---|---|---|---|---|---|")
    runs = study(funding, close, start, end)
    for name, r in runs.items():
        print(row(name, stats(r)))
    always = runs["harvest, always"]
    print("\nharvest, always, by year: " + "; ".join(
        f"{y} {r.mean() * metrics.YEAR:+.1%} a year" for y, r in always.groupby(always.index.year)))
    corr = runs["spot, trend > 0"].corr(always)
    print(f"\nCorrelation of the trend sleeve and the harvest: {corr:+.2f}")

    ret = close.sort_index().pct_change(fill_method=None)
    carry = smoothed(funding).reindex(ret.index)
    for h in (7, 30):
        fwd = np.log1p(ret).rolling(h).sum().shift(-h)
        q = quintile_returns(carry[(carry.index >= start) & (carry.index < end - pd.Timedelta(days=h))], fwd)
        print(f"\nForward {h}-day log return of spot by quintile of smoothed funding (1 lowest):\n\n"
              "| Quintile | Mean | Observations |\n|---|---|---|")
        for k, v in q.iterrows():
            print(f"| {k:.0f} | {v['mean']:+.2%} | {v['count']:.0f} |")
    print("\nLowest fifth minus highest fifth, mean forward log return, 95% interval in blocks of the horizon:\n\n"
          "| Signal | Horizon | Spread | 95% interval | Days |\n|---|---|---|---|---|")
    signals = {"smoothed funding": carry, "smoothed funding, without KCS and ZEC": carry.drop(columns=["KCS", "ZEC"]),
               "smoothed funding against its own history": own_history(carry)}
    for name, sig in signals.items():
        for h in (7, 30):
            fwd = np.log1p(ret).rolling(h).sum().shift(-h)
            s = spread(sig[(sig.index >= start) & (sig.index < end - pd.Timedelta(days=h))], fwd)
            lo, hi = metrics.block_mean_ci(s.to_numpy(), SEED, block=h)
            print(f"| {name} | {h} days | {s.mean():+.2%} | {fmt(lo, True)} to {fmt(hi, True)} | {len(s)} |")


if __name__ == "__main__":
    main()
