"""Causal signals for the generated trend, range, breakout, clock and flow families, pure numpy and pandas.

Each function returns a Series aligned to its input, and the value at bar t reads bars 0..t only, so
computing on a prefix gives the same rows as computing on the whole series (lab/tests/test_channels.py).

- donchian_score: the share of lookbacks whose Donchian state is long, 0 to 1. A lookback n goes long on
  a close above the highest close of the n bars before it and flat on a close below the lowest of the
  n // 2 before it (the Turtle exit); the state holds between those events.
- vol_scale: target over realized annual volatility, capped at 1, so a sleeve is cut when its coin is wild.
- zscore: the close against its own rolling mean, in rolling standard deviations.
- atr_break, keltner_break, squeeze_release: volatility breakouts, a close that clears a range set by recent volatility.
- clock: the candle that closes at a set UTC hour (and weekday), for calendar effects.
- volume_surge, obv_break: volume leading price, for the flow family.

The range family's flat-regime gate is lab/metrics.trend_buckets itself, the one trend labeler.
"""

import math

import numpy as np
import pandas as pd


def donchian_state(close: pd.Series, n: int, side: int = 1) -> pd.Series:
    """1 while lookback n holds a position on `side` (1 long, -1 short), else 0."""
    c = close.astype(float) * side
    enter = c > c.rolling(n).max().shift(1)
    leave = c < c.rolling(max(n // 2, 1)).min().shift(1)
    state = pd.Series(np.where(enter, 1.0, np.where(leave, 0.0, np.nan)), index=close.index)
    return state.ffill().fillna(0.0)


def donchian_score(close: pd.Series, lookbacks: list[int], side: int = 1) -> pd.Series:
    return sum(donchian_state(close, n, side) for n in lookbacks) / len(lookbacks)


def vol_scale(close: pd.Series, target: float, window: int, bars_per_year: float) -> pd.Series:
    """min(1, target / realized annual vol over `window` bars); NaN until the window fills."""
    vol = np.log(close.astype(float)).diff().rolling(window).std() * math.sqrt(bars_per_year)
    return (target / vol).clip(upper=1.0)


def zscore(close: pd.Series, window: int) -> pd.Series:
    c = close.astype(float)
    return (c - c.rolling(window).mean()) / c.rolling(window).std()



def atr(df: pd.DataFrame, window: int) -> pd.Series:
    """Mean true range over `window` bars."""
    prev = df["close"].astype(float).shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def atr_break(df: pd.DataFrame, window: int, k: float) -> pd.Series:
    """The close clears the previous close by k times the ATR of the `window` bars before it."""
    c = df["close"].astype(float)
    return c > c.shift(1) + k * atr(df, window).shift(1)


def keltner_break(df: pd.DataFrame, window: int, k: float) -> pd.Series:
    """The close crosses over its `window` EMA plus k ATR."""
    c = df["close"].astype(float)
    upper = c.ewm(span=window, adjust=False).mean() + k * atr(df, window)
    return (c > upper) & (c.shift(1) <= upper.shift(1))


def squeeze_release(df: pd.DataFrame, window: int, bands: float, keltner: float, within: int) -> pd.Series:
    """Bollinger bands (window, bands) inside Keltner channels (window EMA, keltner ATR) mark a quiet market; true when
    that squeeze ended within the last `within` bars and the close breaks over the upper band."""
    c = df["close"].astype(float)
    mean, sd = c.rolling(window).mean(), c.rolling(window).std(ddof=0)
    mid, width = c.ewm(span=window, adjust=False).mean(), keltner * atr(df, window)
    squeeze = (mean + bands * sd < mid + width) & (mean - bands * sd > mid - width)
    released = squeeze.shift(1, fill_value=False).astype(float).rolling(within).max().eq(1) & ~squeeze
    return released & (c > mean + bands * sd)


def clock(df: pd.DataFrame, hour: int, weekdays: list[int] | None = None) -> pd.Series:
    """True on the candle that closes at `hour`:00 UTC, on one of `weekdays` (Monday 0) when given, so the entry
    fills at that time on any timeframe. The candle length is the smallest gap between candle dates."""
    close = df["date"] + df["date"].diff().min()
    hit = (close.dt.hour == hour) & (close.dt.minute == 0)
    return hit & close.dt.weekday.isin(weekdays) if weekdays is not None else hit


def volume_surge(df: pd.DataFrame, window: int, ratio: float) -> pd.Series:
    """An up candle closing in the top quarter of its range on volume over `ratio` times the mean of the `window`
    bars before it."""
    v = df["volume"].astype(float)
    span = (df["high"] - df["low"]).replace(0, np.nan)
    top = (df["close"] - df["low"]) / span >= 0.75
    return (df["close"] > df["open"]) & top & (v > ratio * v.rolling(window).mean().shift(1))


def obv_break(df: pd.DataFrame, window: int) -> pd.Series:
    """On-balance volume closes over its highest of the `window` bars before while the close does not: volume leads
    price. OBV's starting level cancels out of the comparison, so the signal does not depend on where the data starts."""
    c = df["close"].astype(float)
    obv = (np.sign(c.diff()).fillna(0) * df["volume"].astype(float)).cumsum()
    return (obv > obv.rolling(window).max().shift(1)) & (c <= c.rolling(window).max().shift(1))
