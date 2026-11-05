"""The statistics behind the analysis engine, as pure functions over pandas and numpy.

Conventions: a return is a fraction (0.01 is 1%); a daily series has one row per calendar day,
zeros included; Sharpe and Sortino are annualized with sqrt(365) because crypto trades every day.
Every resampling takes a seed, so a re-run over the same data gives the same numbers.
"""

import hashlib
import math

import numpy as np
import pandas as pd
from scipy.stats import norm

YEAR = 365
EULER_GAMMA = 0.5772156649015329


def seed_of(*parts: object) -> int:
    """A seed that depends only on its parts, not on run order or the process."""
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def daily_returns(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, wallet: float) -> pd.DataFrame:
    """One backtest's closed trades as daily returns on its compounding wallet.

    Each day's return is that day's realized profit over the balance at the start of the day, so
    chunks that each start from `wallet` chain into one series. Days run from start to end,
    end exclusive, and days with no close are zeros.
    """
    days = pd.date_range(utc(start).normalize(), utc(end).normalize(), freq="D", inclusive="left")
    pnl = pd.Series(0.0, index=days)
    count = pd.Series(0, index=days)
    if len(trades):
        by_day = trades.assign(day=pd.to_datetime(trades["close_date"], utc=True).dt.normalize()).groupby("day")
        pnl = pnl.add(by_day["profit_abs"].sum(), fill_value=0.0).reindex(days, fill_value=0.0)
        count = count.add(by_day.size(), fill_value=0).reindex(days, fill_value=0).astype(int)
    before = wallet + pnl.cumsum().shift(1, fill_value=0.0)
    ret = (pnl / before).clip(lower=-1.0).where(before > 0, 0.0)
    return pd.DataFrame({"day": days.date, "ret": ret.to_numpy(), "pnl": pnl.to_numpy(), "trades": count.to_numpy()})


def equity(ret: np.ndarray) -> np.ndarray:
    return np.cumprod(1.0 + np.asarray(ret, dtype=float))


def max_drawdown(ret: np.ndarray) -> float:
    """Largest peak-to-trough fall of the equity curve, as a positive fraction."""
    eq = np.concatenate([[1.0], equity(ret)])
    return float(np.max(1.0 - eq / np.maximum.accumulate(eq)))


def sharpe(ret: np.ndarray) -> float | None:
    r = np.asarray(ret, dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return None if sd == 0 or not np.isfinite(sd) else float(r.mean() / sd * math.sqrt(YEAR))


def sortino(ret: np.ndarray) -> float | None:
    r = np.asarray(ret, dtype=float)
    downside = math.sqrt(np.mean(np.minimum(r, 0.0) ** 2)) if len(r) else 0.0
    return None if downside == 0 else float(r.mean() / downside * math.sqrt(YEAR))


def block_indices(n: int, block: int, samples: int, rng: np.random.Generator, length: int | None = None) -> np.ndarray:
    """Circular block bootstrap: `samples` rows of `length` indices into a series of n, drawn in
    runs of `block` consecutive days so volatility clusters and streaks survive the resample."""
    length = length or n
    blocks = -(-length // block)
    starts = rng.integers(0, n, size=(samples, blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(samples, -1)[:, :length]


def sharpe_ci(ret: np.ndarray, seed: int, samples: int = 1000, block: int = 10, level: float = 0.90):
    r = np.asarray(ret, dtype=float)
    if len(r) < 2 * block:
        return None, None
    res = r[block_indices(len(r), block, samples, np.random.default_rng(seed))]
    sd = res.std(axis=1, ddof=1)
    ok = sd > 0
    if not ok.any():
        return None, None
    s = res.mean(axis=1)[ok] / sd[ok] * math.sqrt(YEAR)
    lo, hi = np.quantile(s, [(1 - level) / 2, (1 + level) / 2])
    return float(lo), float(hi)


def mean_ci(x: np.ndarray, seed: int, samples: int = 1000, level: float = 0.90):
    """Bootstrap interval of a mean (per-trade expectancy), resampled in batches to bound memory."""
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    means = np.concatenate([x[rng.integers(0, len(x), size=(min(100, samples - i), len(x)))].mean(axis=1)
                            for i in range(0, samples, 100)])
    lo, hi = np.quantile(means, [(1 - level) / 2, (1 + level) / 2])
    return float(lo), float(hi)


def exposure(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Share of the window with at least one position open."""
    span = (end - start).total_seconds()
    if not len(trades) or span <= 0:
        return 0.0
    iv = sorted((max(o, start), min(c, end)) for o, c in zip(pd.to_datetime(trades["open_date"], utc=True),
                                                             pd.to_datetime(trades["close_date"], utc=True)))
    covered, cur_s, cur_e = 0.0, None, None
    for s, e in iv:
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                covered += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        covered += (cur_e - cur_s).total_seconds()
    return covered / span


def window_metrics(daily: pd.DataFrame, trades: pd.DataFrame, seed: int) -> dict:
    """Everything the Strategies page shows for one strategy x exchange x window.

    daily: day, ret (one row per day). trades: open_date, close_date, open_rate, close_rate, profit_ratio,
    profit_abs. The research config trades spot, long only, so a trade's edge before fees is close over open.
    """
    r = daily["ret"].to_numpy(dtype=float)
    n = len(r)
    total = float(equity(r)[-1] - 1.0) if n else 0.0
    years = n / YEAR
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if n and total > -1.0 else (-1.0 if n else None)
    mdd = max_drawdown(r) if n else 0.0
    wins = trades.loc[trades["profit_abs"] > 0, "profit_abs"].sum() if len(trades) else 0.0
    losses = -trades.loc[trades["profit_abs"] < 0, "profit_abs"].sum() if len(trades) else 0.0
    exp_lo, exp_hi = mean_ci(trades["profit_ratio"].to_numpy(), seed_of(seed, "expectancy"))
    priced = trades[(trades["open_rate"] > 0) & trades["close_rate"].notna()] if len(trades) else trades
    gross = (priced["close_rate"] / priced["open_rate"] - 1.0).to_numpy(dtype=float) if len(priced) else np.array([])
    gross_lo, gross_hi = mean_ci(gross, seed_of(seed, "gross"))
    sh_lo, sh_hi = sharpe_ci(r, seed_of(seed, "sharpe"))
    months = daily.assign(m=pd.to_datetime(daily["day"]).dt.to_period("M")).groupby("m")["ret"].apply(
        lambda x: float(np.prod(1.0 + x.to_numpy()) - 1.0))
    start = pd.Timestamp(daily["day"].iloc[0], tz="UTC") if n else None
    end = pd.Timestamp(daily["day"].iloc[-1], tz="UTC") + pd.Timedelta(days=1) if n else None
    skew = float(pd.Series(r).skew()) if n > 2 else 0.0
    kurt = float(pd.Series(r).kurt()) + 3.0 if n > 3 else 3.0
    sd = r.std(ddof=1) if n > 1 else 0.0
    return {
        "days": n,
        "start_day": daily["day"].iloc[0] if n else None,
        "end_day": daily["day"].iloc[-1] if n else None,
        "trades": int(len(trades)),
        "total_return": total,
        "cagr": None if cagr is None or not np.isfinite(cagr) else float(cagr),
        "sharpe": sharpe(r),
        "sharpe_lo": sh_lo,
        "sharpe_hi": sh_hi,
        "sortino": sortino(r),
        "calmar": None if not mdd or cagr is None else float(cagr / mdd),
        "max_drawdown": mdd,
        "profit_factor": None if losses == 0 else float(wins / losses),
        "expectancy": float(trades["profit_ratio"].mean()) if len(trades) else None,
        "expectancy_lo": exp_lo,
        "expectancy_hi": exp_hi,
        "gross_expectancy": float(gross.mean()) if len(gross) else None,
        "gross_expectancy_lo": gross_lo,
        "gross_expectancy_hi": gross_hi,
        "fee_per_trade": float((gross - priced["profit_ratio"].to_numpy(dtype=float)).mean()) if len(gross) else None,
        "winrate": float((trades["profit_abs"] > 0).mean()) if len(trades) else None,
        "exposure": exposure(trades, start, end) if n else 0.0,
        "months_profitable": float((months > 0).mean()) if len(months) else None,
        # inputs to the deflated Sharpe, which needs every trial in the window first
        "_sr_daily": None if sd == 0 or n < 2 else float(r.mean() / sd),
        "_skew": skew,
        "_kurt": kurt,
    }


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, trial_srs: list[float]) -> float | None:
    """Bailey and Lopez de Prado's deflated Sharpe ratio: the probability that the true Sharpe is
    above the best one expected by luck alone among this many trials. All Sharpes per period."""
    if sr is None or n < 3:
        return None
    srs = [s for s in trial_srs if s is not None]
    k = len(srs)
    if k >= 2:
        var = float(np.var(srs, ddof=1))
        sr0 = math.sqrt(var) * ((1 - EULER_GAMMA) * norm.ppf(1 - 1 / k) + EULER_GAMMA * norm.ppf(1 - 1 / (k * math.e)))
    else:
        sr0 = 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    return float(norm.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)))


def monte_carlo(ret: np.ndarray, horizon: int, seed: int, paths: int = 5000, block: int = 10) -> dict:
    """Projected return bands over `horizon` days from a block bootstrap of past daily returns."""
    r = np.asarray(ret, dtype=float)
    res = r[block_indices(len(r), block, paths, np.random.default_rng(seed), horizon)]
    eq = np.cumprod(1.0 + res, axis=1)
    total = eq[:, -1] - 1.0
    peak = np.maximum.accumulate(np.concatenate([np.ones((paths, 1)), eq], axis=1), axis=1)[:, 1:]
    mdd = np.max(1.0 - eq / peak, axis=1)
    p10, p50, p90 = np.quantile(total, [0.1, 0.5, 0.9])
    return {"p10": float(p10), "p50": float(p50), "p90": float(p90), "prob_loss": float((total < 0).mean()),
            "exp_max_drawdown": float(mdd.mean()), "paths": paths}


def regimes(close: pd.Series) -> pd.DataFrame:
    """Market regime per day from BTC daily closes (index: day). Trend is the close against its
    200-day mean; volatility is 30-day realized volatility, cut into terciles of its own history.
    The terciles use the whole history, so they describe the past, they do not forecast."""
    close = close.sort_index().astype(float)
    sma = close.rolling(200).mean()
    vol = np.log(close).diff().rolling(30).std() * math.sqrt(YEAR)
    trend = pd.Series(np.where(close > sma, "bull", "bear"), index=close.index).where(sma.notna())
    lo, hi = vol.quantile([1 / 3, 2 / 3])
    bucket = pd.Series(np.select([vol <= lo, vol <= hi], ["low", "mid"], "high"), index=close.index).where(vol.notna())
    return pd.DataFrame({"day": close.index, "btc_close": close.to_numpy(), "trend": trend.to_numpy(),
                         "vol_30d": vol.to_numpy(), "vol_bucket": bucket.to_numpy()})
