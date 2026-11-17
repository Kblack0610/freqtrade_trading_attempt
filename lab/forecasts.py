"""Carver's forecasts for the forecast family (docs/carver-afts.md), pure numpy and pandas.

Each function takes the close and returns a Series aligned to it; the value at bar t reads bars 0..t only
(lab/tests/test_forecasts.py). A forecast averages 10 in size, is capped at 20 and is above 0 for a long: the
strategy enters on it and sizes the stake by it. The EWMAC scalars are Carver's published values; a multiplier or
scalar marked approximate is to be confirmed against the book. Kept apart from lab/channels.py so that a change here
does not change the hash of, and so rerun, every trend, range, breakout, clock and flow strategy.
"""

import numpy as np
import pandas as pd

CAP = 20.0
# Carver's published forecast scalars for EWMAC(n, 4n), by the fast span n: they bring the mean absolute forecast to 10.
EWMAC_SCALAR = {2: 10.6, 4: 7.5, 8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}


def price_vol(close: pd.Series, span: int = 35) -> pd.Series:
    """Exponentially weighted standard deviation of one-bar price changes, in price units; NaN until `span` bars."""
    return close.astype(float).diff().ewm(span=span, min_periods=span).std()


def hold_forecast(close: pd.Series, level: float = 10.0) -> pd.Series:
    """A constant forecast: always long, sized by the volatility scale alone (AFTS 2, buy and hold with risk scaling)."""
    return pd.Series(level, index=close.index)


def ewmac(close: pd.Series, fast: int) -> pd.Series:
    """EWMAC(fast, 4 * fast) over price volatility, times its scalar, capped at +-20 (AFTS 5, 7, 8)."""
    c = close.astype(float)
    raw = (c.ewm(span=fast, adjust=False).mean() - c.ewm(span=4 * fast, adjust=False).mean()) / price_vol(c)
    return (raw * EWMAC_SCALAR[fast]).clip(-CAP, CAP)


def ewmac_mix(close: pd.Series, fasts: list[int], fdm: float) -> pd.Series:
    """The mean of several capped EWMAC forecasts times the diversification multiplier, capped again (AFTS 9)."""
    return (sum(ewmac(close, n) for n in fasts) / len(fasts) * fdm).clip(-CAP, CAP)


def vol_multiplier(close: pd.Series, history: int, smooth: int = 10) -> pd.Series:
    """2 when volatility is the lowest of the last `history` bars, down to 0.5 at the highest, smoothed (AFTS 13).
    NaN until `history` bars exist. Carver ranks against all history; a fixed window keeps the cost linear."""
    vol = close.astype(float).pct_change().ewm(span=35, min_periods=35).std().to_numpy()
    q = np.full(len(vol), np.nan)
    if len(vol) >= history:
        windows = np.lib.stride_tricks.sliding_window_view(vol, history)
        for lo in range(0, len(windows), 2000):
            w = windows[lo:lo + 2000]
            q[history - 1 + lo:history - 1 + lo + len(w)] = (w <= w[:, -1:]).mean(axis=1)
    mult = 2.0 - 1.5 * pd.Series(q, index=close.index)
    return mult.ewm(span=smooth, adjust=False, ignore_na=True).mean().where(mult.notna())


def regime_mix(close: pd.Series, fasts: list[int], fdm: float, history: int) -> pd.Series:
    """ewmac_mix times vol_multiplier, capped (AFTS 13)."""
    return (ewmac_mix(close, fasts, fdm) * vol_multiplier(close, history)).clip(-CAP, CAP)


def breakout_forecast(close: pd.Series, window: int) -> pd.Series:
    """Where the close sits in the rolling high-low range of `window` bars, -20 at the bottom to +20 at the top,
    smoothed over a quarter of the window (AFTS 21)."""
    c = close.astype(float)
    hi, lo = c.rolling(window).max(), c.rolling(window).min()
    span = (hi - lo).replace(0, np.nan)
    raw = 40.0 * (c - (hi + lo) / 2) / span
    return raw.ewm(span=max(window // 4, 1), adjust=False, ignore_na=True).mean().where(raw.notna())


def acceleration(close: pd.Series, fast: int, scalar: float) -> pd.Series:
    """The change in the EWMAC(fast, 4 * fast) trend over `fast` bars, in volatility units: a trend speeding up
    (AFTS 23). The scalar is approximate."""
    c = close.astype(float)
    raw = (c.ewm(span=fast, adjust=False).mean() - c.ewm(span=4 * fast, adjust=False).mean()) / price_vol(c)
    return ((raw - raw.shift(fast)) * scalar).clip(-CAP, CAP)


def skew_forecast(close: pd.Series, window: int, scalar: float) -> pd.Series:
    """Negative rolling skew of one-bar returns: positive, so long, after a stretch of downside tails (AFTS 24).
    The scalar is approximate."""
    return (-close.astype(float).pct_change().rolling(window).skew() * scalar).clip(-CAP, CAP)


def dip_forecast(close: pd.Series, eq_days: float, vol_days: float, scalar: float, bars_per_day: float,
                 trend_fast: int = 0, floor: float = 0.0) -> pd.Series:
    """The gap under an `eq_days` EWMA equilibrium over price volatility, positive when the close is below it
    (AFTS 26). With trend_fast set it stands aside, forecast 0, while EWMAC(trend_fast) is under `floor`: a strong
    trend running against the dip (AFTS 27). The scalar is approximate."""
    c = close.astype(float)
    eq = c.ewm(span=max(round(eq_days * bars_per_day), 1), adjust=False).mean()
    vspan = max(round(vol_days * bars_per_day), 2)
    out = ((eq - c) / c.diff().ewm(span=vspan, min_periods=vspan).std() * scalar).clip(-CAP, CAP)
    if trend_fast:
        out = out.where(ewmac(c, trend_fast) >= floor, 0.0)
    return out
