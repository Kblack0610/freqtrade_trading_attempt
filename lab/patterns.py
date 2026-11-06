"""Causal chart-pattern detection over OHLC frames, pure numpy and pandas.

Every function takes a frame with open, high, low and close columns and returns Series aligned to
its index. Each value at bar t depends only on bars 0..t: a swing pivot at bar i is known only at
bar i + k, once k later bars have failed to exceed it, and a signal at t reads pattern state as of
t - 1 plus the close at t. So computing on df[:t] gives the same rows as computing on all of df,
which lab/tests/test_patterns.py checks for every function.

Every pattern also looks back a bounded number of bars (its `window` plus the pivot width), so a
strategy's startup_candle_count can cover it and freqtrade's recursive-analysis stays clean.

Long signals: resistance_break, double_bottom, triangle_break(direction="up"), bull_flag.
Short signals, kept for symmetry and exits: support_break, double_top,
triangle_break(direction="down"), bear_flag.
"""

import numpy as np
import pandas as pd

TRIANGLES = ("ascending", "descending", "symmetric")


def swing_pivots(df: pd.DataFrame, k: int = 5) -> pd.DataFrame:
    """Swing highs and lows, each reported on the bar that confirms it.

    Bar i is a swing high when its high is at least the k highs before it and above the k after
    it; it is confirmed at bar i + k. Ties go to the later bar. Columns: pivot_high and pivot_low
    hold the pivot's price on its confirmation bar and NaN elsewhere, so the pivot itself sits k
    rows earlier.
    """
    out = pd.DataFrame(index=df.index)
    for col, side in (("high", 1), ("low", -1)):
        s = df[col] * side
        cand = s.shift(k)
        left = s.shift(k + 1).rolling(k).max()
        right = s.rolling(k).max()
        is_pivot = (cand >= left) & (cand > right)
        out[f"pivot_{col}"] = (cand * side).where(is_pivot)
    return out


def last_level(df: pd.DataFrame, side: str, k: int = 5, max_age: int = 120) -> pd.Series:
    """The last confirmed swing high (side "high") or low ("low"), held for max_age bars after it
    was confirmed, then NaN."""
    return swing_pivots(df, k)[f"pivot_{side}"].ffill(limit=max_age)


def _cross(close: pd.Series, level: pd.Series, up: bool) -> pd.Series:
    """Close crosses the level known at the previous bar: below or at it on t - 1, beyond it on t."""
    prev = level.shift(1)
    if up:
        return (close > prev) & (close.shift(1) <= prev)
    return (close < prev) & (close.shift(1) >= prev)


def resistance_break(df: pd.DataFrame, k: int = 5, max_age: int = 120) -> pd.Series:
    """Close crosses above the last confirmed swing high."""
    return _cross(df["close"], last_level(df, "high", k, max_age), up=True)


def support_break(df: pd.DataFrame, k: int = 5, max_age: int = 120) -> pd.Series:
    """Close crosses below the last confirmed swing low."""
    return _cross(df["close"], last_level(df, "low", k, max_age), up=False)


def _confirmed(piv: pd.DataFrame, side: str, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Bar positions and prices of the confirmed pivots on one side of swing_pivots(df, k), in
    confirmation order."""
    p = piv[f"pivot_{side}"].to_numpy()
    conf = np.flatnonzero(~np.isnan(p))
    return conf - k, p[conf]


def _recent(idx: np.ndarray, t: int, k: int, window: int, n: int) -> slice:
    """The last n pivots confirmed by bar t - 1 whose pivot bar lies within window bars of t."""
    hi = np.searchsorted(idx + k, t - 1, side="right")
    lo = max(np.searchsorted(idx, t - window, side="left"), hi - n)
    return slice(lo, hi)


def _double(df: pd.DataFrame, bottom: bool, k: int, window: int, tol: float, min_depth: float) -> pd.Series:
    close = df["close"].to_numpy()
    extreme = df["high" if bottom else "low"].to_numpy()
    idx, price = _confirmed(swing_pivots(df, k), "low" if bottom else "high", k)
    sig = np.zeros(len(df), dtype=bool)
    for t in range(1, len(df)):
        s = _recent(idx, t, k, window, 2)
        if s.stop - s.start < 2:
            continue
        (i1, i2), (p1, p2) = idx[s], price[s]
        if abs(p2 - p1) > tol * min(p1, p2):
            continue
        span = extreme[i1:i2 + 1]
        neck = span.max() if bottom else span.min()
        since = close[i2:t]
        if bottom:
            sig[t] = (neck - max(p1, p2) >= min_depth * neck and since.max() <= neck < close[t])
        else:
            sig[t] = (min(p1, p2) - neck >= min_depth * neck and since.min() >= neck > close[t])
    return pd.Series(sig, index=df.index)


def double_bottom(df: pd.DataFrame, k: int = 5, window: int = 120, tol: float = 0.01,
                  min_depth: float = 0.02) -> pd.Series:
    """The last two confirmed swing lows within window bars sit within tol of each other, the
    highest high between them (the neckline) is at least min_depth above the higher low, and the
    close crosses above the neckline for the first time since the second low."""
    return _double(df, True, k, window, tol, min_depth)


def double_top(df: pd.DataFrame, k: int = 5, window: int = 120, tol: float = 0.01,
               min_depth: float = 0.02) -> pd.Series:
    """Mirror of double_bottom: two swing highs, the lowest low between them, a close below it."""
    return _double(df, False, k, window, tol, min_depth)


def _line(idx: np.ndarray, price: np.ndarray) -> tuple[float, float, float]:
    """Least-squares line through the pivots: slope per bar, intercept, and the relative change the
    line makes across the pivots' span (slope x span / mean price)."""
    x = idx.astype(float)
    xm, ym = x.mean(), price.mean()
    slope = ((x - xm) * (price - ym)).sum() / ((x - xm) ** 2).sum()
    return slope, ym - slope * xm, slope * (x[-1] - x[0]) / ym


def _shape(rel_high: float, rel_low: float, flat: float) -> str | None:
    if abs(rel_high) <= flat and rel_low > flat:
        return "ascending"
    if rel_high < -flat and abs(rel_low) <= flat:
        return "descending"
    if rel_high < -flat and rel_low > flat:
        return "symmetric"
    return None


def triangle_break(df: pd.DataFrame, kind: str, direction: str = "up", k: int = 5, n: int = 3,
                   window: int = 120, flat: float = 0.01) -> pd.Series:
    """A close through a converging triangle.

    At each bar the last n confirmed swing highs and the last n swing lows within window bars get a
    least-squares line each. A line is flat when it moves at most `flat` (relative) across its
    pivots, rising or falling beyond that. ascending: flat top, rising bottom; descending: falling
    top, flat bottom; symmetric: falling top, rising bottom. The signal is the close crossing the
    upper line (direction "up") or the lower line ("down"), with the lines as known at t - 1 and the
    upper line still above the lower one.
    """
    if kind not in TRIANGLES or direction not in ("up", "down"):
        raise ValueError(f"kind must be one of {TRIANGLES}, direction up or down")
    close = df["close"].to_numpy()
    piv = swing_pivots(df, k)
    hidx, hp = _confirmed(piv, "high", k)
    lidx, lp = _confirmed(piv, "low", k)
    sig = np.zeros(len(df), dtype=bool)
    cache: dict[tuple, tuple | None] = {}
    for t in range(1, len(df)):
        sh, sl = _recent(hidx, t, k, window, n), _recent(lidx, t, k, window, n)
        if sh.stop - sh.start < n or sl.stop - sl.start < n:
            continue
        key = (sh.start, sh.stop, sl.start, sl.stop)
        if key not in cache:
            upper, lower = _line(hidx[sh], hp[sh]), _line(lidx[sl], lp[sl])
            cache[key] = (upper, lower) if _shape(upper[2], lower[2], flat) == kind else None
        lines = cache[key]
        if lines is None:
            continue
        (a_u, b_u, _), (a_l, b_l, _) = lines
        u_prev, u_now = a_u * (t - 1) + b_u, a_u * t + b_u
        l_prev, l_now = a_l * (t - 1) + b_l, a_l * t + b_l
        if u_now <= l_now:
            continue
        if direction == "up":
            sig[t] = close[t - 1] <= u_prev and close[t] > u_now
        else:
            sig[t] = close[t - 1] >= l_prev and close[t] < l_now
    return pd.Series(sig, index=df.index)


def _flag(df: pd.DataFrame, bull: bool, pole: int, flag: int, impulse: float, retrace: float) -> pd.Series:
    close = df["close"]
    side = 1 if bull else -1
    # The pole ends on the bar before the flag starts; the flag is the `flag` bars before t.
    pole_end, pole_start = close.shift(flag + 1), close.shift(flag + 1 + pole)
    move = side * (pole_end / pole_start - 1)
    height = (pole_end - pole_start).abs()
    top = df["high"].shift(1).rolling(flag).max()
    bottom = df["low"].shift(1).rolling(flag).min()
    # The sign of the flag closes' least-squares slope, each window on its own: a rolling cov
    # against a growing bar counter loses precision and depends on where the frame starts.
    prev = close.shift(1).to_numpy()
    slope = np.full(len(df), np.nan)
    if len(df) > flag:
        w = np.arange(flag) - (flag - 1) / 2
        slope[flag:] = np.lib.stride_tricks.sliding_window_view(prev[1:], flag) @ w
    tight = (top - bottom) <= retrace * height
    counter = side * pd.Series(slope, index=df.index) <= 0
    broke = close > top if bull else close < bottom
    return (move >= impulse) & tight & counter & broke


def bull_flag(df: pd.DataFrame, pole: int = 10, flag: int = 8, impulse: float = 0.05,
              retrace: float = 0.5) -> pd.Series:
    """A rise of at least `impulse` over `pole` bars, then `flag` bars drifting flat or down inside a
    range at most `retrace` of the pole's height, then a close above that range."""
    return _flag(df, True, pole, flag, impulse, retrace)


def bear_flag(df: pd.DataFrame, pole: int = 10, flag: int = 8, impulse: float = 0.05,
              retrace: float = 0.5) -> pd.Series:
    """Mirror of bull_flag: a fall, a flat or rising tight flag, a close below it."""
    return _flag(df, False, pole, flag, impulse, retrace)
