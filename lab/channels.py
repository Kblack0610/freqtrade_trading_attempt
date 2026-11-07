"""Causal channel and range signals for the staged trend and range families, pure numpy and pandas.

Each function returns a Series aligned to its input, and the value at bar t reads bars 0..t only, so
computing on a prefix gives the same rows as computing on the whole series (lab/tests/test_channels.py).

- donchian_score: the share of lookbacks whose Donchian state is long, 0 to 1. A lookback n goes long on
  a close above the highest close of the n bars before it and flat on a close below the lowest of the
  n // 2 before it (the Turtle exit); the state holds between those events.
- vol_scale: target over realized annual volatility, capped at 1, so a sleeve is cut when its coin is wild.
- zscore: the close against its own rolling mean, in rolling standard deviations.

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

