"""The statistics behind the analysis engine, as pure functions over pandas and numpy.

Conventions: a return is a fraction (0.01 is 1%); a daily series has one row per calendar day,
zeros included; Sharpe and Sortino are annualized with sqrt(365) because crypto trades every day.
Every resampling takes a seed, so a re-run over the same data gives the same numbers.
"""

import hashlib
import itertools
import math

import numpy as np
import pandas as pd
from scipy.stats import norm

from lab.util import utc

YEAR = 365
EULER_GAMMA = 0.5772156649015329


def seed_of(*parts: object) -> int:
    """A seed that depends only on its parts, not on run order or the process. 32 bits, the most
    numpy's RandomState, and so hyperopt's optuna samplers, accept."""
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


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


def worst_open(trades: pd.DataFrame) -> float | None:
    """The deepest any trade went against its entry, from the candle lows it lived through. Returns
    and drawdown are realized on close, so a strategy that holds its losers shows neither until a stop hits."""
    if not len(trades):
        return None
    loss = (trades["min_rate"].astype(float) / trades["open_rate"].astype(float) - 1.0).min()
    return None if pd.isna(loss) else float(loss)


def capital_exposure(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Mean share of the wallet in positions over the window: each trade's `share` of its wallet,
    weighted by its time inside the window. `exposure` counts a day with one trade of three as fully
    in the market; this counts a third."""
    span = (end - start).total_seconds()
    if not len(trades) or span <= 0:
        return 0.0
    opened = pd.to_datetime(trades["open_date"], utc=True).clip(lower=start)
    closed = pd.to_datetime(trades["close_date"], utc=True).clip(upper=end)
    held = (closed - opened).dt.total_seconds().clip(lower=0)
    return min(1.0, float((trades["share"] * held).sum() / span))


def block_sample(x: np.ndarray, block: int, samples: int, rng: np.random.Generator, length: int | None = None) -> np.ndarray:
    """Circular block bootstrap: `samples` rows of `length` values of x, drawn in runs of `block` consecutive days so
    volatility clusters and streaks survive the resample. The runs are read as windows over x with its first values
    appended, so no index array is built (the profile's top cost); block_sample(np.arange(n), ...) gives the indices."""
    x = np.asarray(x)
    n = len(x)
    length = length or n
    starts = rng.integers(0, n, size=(samples, -(-length // block)))
    windows = np.lib.stride_tricks.sliding_window_view(np.concatenate([x, np.resize(x, block - 1)]), block)
    return windows[starts].reshape(samples, -1)[:, :length]


def sharpe_ci(ret: np.ndarray, seed: int, samples: int = 1000, block: int = 10, level: float = 0.90):
    r = np.asarray(ret, dtype=float)
    if len(r) < 2 * block:
        return None, None
    res = block_sample(r, block, samples, np.random.default_rng(seed))
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


def block_mean_ci(x: np.ndarray, seed: int, samples: int = 1000, block: int = 10, level: float = 0.95):
    """mean_ci in blocks of consecutive trades, since trades that overlap or follow each other share a market."""
    x = np.asarray(x, dtype=float)
    if len(x) < 2 * block:
        return None, None
    means = block_sample(x, block, samples, np.random.default_rng(seed)).mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, (1 + level) / 2])
    return float(lo), float(hi)


def diff_ci(a: np.ndarray, b: np.ndarray, seed: int, samples: int = 1000, level: float = 0.90):
    """Bootstrap interval of mean(a) - mean(b) for two independent samples of trades."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 2 or len(b) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    diffs = np.concatenate([a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1)
                            - b[rng.integers(0, len(b), size=(n, len(b)))].mean(axis=1)
                            for n in (min(100, samples - i) for i in range(0, samples, 100))])
    lo, hi = np.quantile(diffs, [(1 - level) / 2, (1 + level) / 2])
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


def hold_excess(daily: pd.DataFrame, close: pd.Series, capital: float, seed: int, samples: int = 1000, block: int = 10,
                level: float = 0.90) -> tuple:
    """Annual return over holding BTC with the same share of capital, with a block-bootstrap interval.

    daily: day, ret. close: BTC daily close by day. Each day's excess is the strategy's return less
    `capital` times BTC's, so a long-only strategy that only rode the market scores about zero.
    """
    both = daily.set_index("day")[["ret"]].join(close.pct_change().rename("btc"), how="inner").dropna()
    if len(both) < 2 * block:
        return None, None, None
    x = (both["ret"] - capital * both["btc"]).to_numpy(dtype=float)
    means = block_sample(x, block, samples, np.random.default_rng(seed)).mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, (1 + level) / 2])
    return float(x.mean() * YEAR), float(lo * YEAR), float(hi * YEAR)


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
        "worst_open": worst_open(trades),
        "median_hold_hours": float((pd.to_datetime(trades["close_date"]) - pd.to_datetime(trades["open_date"]))
                                   .median() / pd.Timedelta(hours=1)) if len(trades) else None,
        "months_profitable": float((months > 0).mean()) if len(months) else None,
        "trades_needed": trades_needed(trades["profit_ratio"].to_numpy()) if len(trades) else None,
        # inputs to the deflated Sharpe, which needs every trial in the window first
        "_sr_daily": None if sd == 0 or n < 2 else float(r.mean() / sd),
        "_skew": skew,
        "_kurt": kurt,
    }


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, trials: int) -> float | None:
    """Bailey and Lopez de Prado's deflated Sharpe ratio: the probability that the true Sharpe is
    above the best one `trials` strategies with a true Sharpe of zero would show by luck. Sharpes
    are per period over n periods. Under that null each estimate's variance is 1 / (n - 1); the
    spread of the trials' own estimates would also count real differences, such as fee drag, as luck.
    """
    if sr is None or n < 3:
        return None
    if trials >= 2:
        emax = (1 - EULER_GAMMA) * norm.ppf(1 - 1 / trials) + EULER_GAMMA * norm.ppf(1 - 1 / (trials * math.e))
        sr0 = emax / math.sqrt(n - 1)
    else:
        sr0 = 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    return float(norm.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)))


def min_backtest_years(sharpe: float | None, trials: int) -> float | None:
    """Bailey, Borwein, Lopez de Prado and Zhu (2014), Theorem 3.1, the bound 2 ln(N) / SR^2 on the minimum backtest
    length in years: with fewer years than this, the best of N skill-less strategies shows this annualized Sharpe by
    luck. None when the Sharpe is missing or not above zero, since no length is enough then."""
    if sharpe is None or not math.isfinite(sharpe) or sharpe <= 0:
        return None
    return 2 * math.log(trials) / sharpe ** 2 if trials >= 2 else 0.0


def effective_trials(returns: pd.DataFrame, min_overlap: int = 30) -> float:
    """How many independent trials a set of return series is worth: the participation ratio of the
    eigenvalues of their correlation matrix, (sum l)^2 / sum l^2. Independent series count once each,
    identical ones count once together. Pairs that overlap fewer than min_overlap days count as independent.
    """
    k = returns.shape[1]
    if k < 2:
        return float(k)
    rho = returns.corr(min_periods=min_overlap).to_numpy(dtype=float)
    rho = np.where(np.isfinite(rho), rho, 0.0)
    np.fill_diagonal(rho, 1.0)
    lam = np.clip(np.linalg.eigvalsh(rho), 0.0, None)
    return float(lam.sum() ** 2 / (lam ** 2).sum())


def pbo(returns: np.ndarray, blocks: int = 16) -> dict | None:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey,
    Borwein, Lopez de Prado and Zhu 2017). returns is days x strategies. The days split into `blocks`
    contiguous blocks; every half of them is in sample once and the rest out of sample. In each split
    the strategy with the best in-sample Sharpe is ranked out of sample, and the logit of its relative
    rank is negative when it lands below the median. PBO is the share of splits where it does.
    """
    t, n = returns.shape
    if n < 2 or t < blocks * 2:
        return None
    parts = np.array_split(np.arange(t), blocks)
    s1 = np.stack([returns[p].sum(axis=0) for p in parts])
    s2 = np.stack([(returns[p] ** 2).sum(axis=0) for p in parts])
    cnt = np.array([len(p) for p in parts], dtype=float)
    combos = np.array([[i in c for i in range(blocks)] for c in itertools.combinations(range(blocks), blocks // 2)],
                      dtype=float)

    def sharpe_of(mask: np.ndarray) -> np.ndarray:
        m = mask @ cnt
        mean = (mask @ s1) / m[:, None]
        var = (mask @ s2) / m[:, None] - mean ** 2
        sd = np.sqrt(np.clip(var, 0.0, None))
        return np.divide(mean, sd, out=np.zeros_like(mean), where=sd > 0)

    is_sr, oos_sr = sharpe_of(combos), sharpe_of(1.0 - combos)
    best = is_sr.argmax(axis=1)
    picked = oos_sr[np.arange(len(combos)), best]
    below = (oos_sr < picked[:, None]).sum(axis=1)
    ties = (oos_sr == picked[:, None]).sum(axis=1)
    omega = (below + (ties + 1) / 2) / (n + 1)
    logit = np.log(omega / (1 - omega))
    return {"pbo": float((logit <= 0).mean()), "logit_median": float(np.median(logit)), "splits": len(combos)}


def shrink(x: np.ndarray, se: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Empirical-Bayes posterior mean and sd of each true mean, from estimates x with standard errors se. The
    spread of true means is DerSimonian and Laird's moment estimate, and the posterior sd carries the uncertainty
    in the population mean. That mean is capped at zero: a market rising under every long-only strategy lifts
    the whole population, and it is no reason to believe any one of them.
    """
    w = 1 / se ** 2
    q = (w * (x - (w * x).sum() / w.sum()) ** 2).sum()
    tau2 = max(0.0, (q - (len(x) - 1)) / (w.sum() - (w ** 2).sum() / w.sum()))
    w = 1 / (se ** 2 + tau2)
    mu, mu_var = min((w * x).sum() / w.sum(), 0.0), 1 / w.sum()
    b = se ** 2 / (se ** 2 + tau2)
    return (1 - b) * x + b * mu, np.sqrt((1 - b) * se ** 2 + b ** 2 * mu_var)


def trades_needed(x: np.ndarray, z: float = 1.645) -> int | None:
    """More trades needed before the low end of a 90% interval on the mean per-trade return clears
    zero, if the mean and spread hold: (z sd / mean)^2 less the trades so far. None when the mean is
    not positive, since then no number of trades gets there.
    """
    x = np.asarray(x, dtype=float)
    if len(x) < 2 or x.mean() <= 0:
        return None
    return max(0, math.ceil((z * x.std(ddof=1) / x.mean()) ** 2) - len(x))


def _paths(ret: np.ndarray, horizon: int, seed: int, paths: int, block: int) -> tuple[np.ndarray, np.ndarray]:
    """Each bootstrapped path's total return and its deepest drawdown over `horizon` steps."""
    r = np.asarray(ret, dtype=float)
    res = block_sample(r, block, paths, np.random.default_rng(seed), horizon)
    eq = np.cumprod(1.0 + res, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.ones((paths, 1)), eq], axis=1), axis=1)[:, 1:]
    return eq[:, -1] - 1.0, np.max(1.0 - eq / peak, axis=1)


def monte_carlo(ret: np.ndarray, horizon: int, seed: int, paths: int = 5000, block: int = 10) -> dict:
    """Projected return bands over `horizon` days from a block bootstrap of past daily returns."""
    total, mdd = _paths(ret, horizon, seed, paths, block)
    p10, p50, p90 = np.quantile(total, [0.1, 0.5, 0.9])
    return {"p10": float(p10), "p50": float(p50), "p90": float(p90), "prob_loss": float((total < 0).mean()),
            "exp_max_drawdown": float(mdd.mean()), "paths": paths}


def projection(ret: np.ndarray, horizon: int, seed: int, paths: int = 5000, block: int = 3) -> dict:
    """The 5th, 50th and 95th percentile return over `horizon` months from a block bootstrap of monthly returns."""
    total, mdd = _paths(ret, horizon, seed, paths, block)
    p5, p50, p95 = np.quantile(total, [0.05, 0.5, 0.95])
    return {"p5": float(p5), "p50": float(p50), "p95": float(p95), "prob_loss": float((total < 0).mean()),
            "exp_max_drawdown": float(mdd.mean()), "paths": paths}


def reorder_drawdown(ret: np.ndarray, seed: int, level: float = 0.95, paths: int = 2000) -> float:
    """The drawdown at `level` when the same trades arrive in a random order, resampled one at a time."""
    return float(np.quantile(_paths(ret, len(ret), seed, paths, 1)[1], level))


def regimes(close: pd.Series, band: float, vol_min_days: int) -> pd.DataFrame:
    """Market regime per day from BTC daily closes (index: day), known at that day's close and never revised.
    Trend is trend_buckets; volatility is 30-day realized volatility cut at the terciles of the volatility seen
    so far (expanding window), so appending later days never changes an earlier label. A day gets no volatility
    bucket until `vol_min_days` volatilities have been seen."""
    close = close.sort_index().astype(float)
    vol = np.log(close).diff().rolling(30).std() * math.sqrt(YEAR)
    seen = vol.expanding(min_periods=vol_min_days)
    lo, hi = seen.quantile(1 / 3), seen.quantile(2 / 3)
    bucket = pd.Series(np.select([vol <= lo, vol <= hi], ["low", "mid"], "high"), index=close.index).where(lo.notna())
    return pd.DataFrame({"day": close.index, "btc_close": close.to_numpy(), "trend": trend_buckets(close, band).to_numpy(),
                         "vol_30d": vol.to_numpy(), "vol_bucket": bucket.to_numpy()})


def trend_buckets(close: pd.Series, band: float) -> pd.Series:
    """up, down or flat per day: the BTC close against its 200-day mean, flat within `band` of it."""
    gap = close.sort_index().astype(float) / close.sort_index().astype(float).rolling(200).mean() - 1.0
    return pd.Series(np.select([gap > band, gap < -band], ["up", "down"], "flat"), index=gap.index).where(gap.notna())


def drift(live: pd.DataFrame, bt: pd.DataFrame, tolerance: pd.Timedelta) -> dict:
    """A league bot's dry run against a backtest of the same strategy, exchange and period.

    live: pair, open_date, is_open, profit_ratio, profit_abs. bt: pair, open_date, exit_reason,
    profit_ratio, profit_abs. A live entry matches the backtest entry on the same pair nearest in
    time, within `tolerance` (one candle). A backtest trade still open at the end is closed there
    with exit_reason force_exit, so it counts as an entry but not as a closed trade. The gap is the
    mean live minus backtest profit ratio over matched trades closed on both sides.
    """
    lc = live[~live["is_open"].astype(bool)] if len(live) else live
    bc = bt[bt["exit_reason"] != "force_exit"] if len(bt) else bt
    out = {"live_entries": len(live), "bt_entries": len(bt), "matched": 0, "live_closed": len(lc), "bt_closed": len(bc),
           "live_expectancy": float(lc["profit_ratio"].mean()) if len(lc) else None,
           "bt_expectancy": float(bc["profit_ratio"].mean()) if len(bc) else None,
           "live_profit": float(lc["profit_abs"].sum()) if len(lc) else 0.0,
           "bt_profit": float(bc["profit_abs"].sum()) if len(bc) else 0.0, "gap": None, "gap_trades": 0, "gaps": []}
    if not len(live) or not len(bt):
        return out
    side = bt[["pair", "open_date", "profit_ratio", "exit_reason"]].rename(
        columns={"profit_ratio": "bt_ratio", "exit_reason": "bt_exit"})
    # pandas keeps each source's time resolution, and merge_asof needs one
    side = side.assign(open_date=pd.to_datetime(side["open_date"], utc=True).astype("datetime64[ns, UTC]"),
                       bt_open=lambda x: x["open_date"])
    mine = live.assign(open_date=pd.to_datetime(live["open_date"], utc=True).astype("datetime64[ns, UTC]"))
    m = pd.merge_asof(mine.sort_values("open_date"), side.sort_values("open_date"), on="open_date", by="pair",
                      tolerance=tolerance, direction="nearest")
    hit = m[m["bt_open"].notna()]
    both = hit[~hit["is_open"].astype(bool) & (hit["bt_exit"] != "force_exit")]
    gaps = (both["profit_ratio"] - both["bt_ratio"]).astype(float)
    out.update(matched=len(hit), gap_trades=len(both), gap=float(gaps.mean()) if len(both) else None, gaps=gaps.tolist())
    return out


def confidence_sequence(x, lo: float, hi: float, alpha: float, grid: int = 400) -> tuple[np.ndarray, np.ndarray]:
    """Anytime-valid interval for the mean of values bounded in [lo, hi]: entry t of each array bounds the true mean
    after the first t+1 values, and with probability 1 - alpha no entry ever excludes it, however often it is read
    (Ville's inequality).

    The betting confidence sequence of Waudby-Smith and Ramdas (JRSS B 2024), hedged over both signs, with the
    predictable plug-in bet that adapts to the running variance. A candidate mean m is out once either capital
    process, bet against m, has ever reached 1/alpha; the bounds are the surviving grid cells, one cell wider on each
    side. Values outside [lo, hi] are clipped, so the bounds assume the range holds."""
    y = (np.clip(np.asarray(x, dtype=float), lo, hi) - lo) / (hi - lo)
    n = len(y)
    m = np.linspace(0.0, 1.0, grid + 1)[1:-1]
    if not n:
        return np.empty(0), np.empty(0)
    logk_up, logk_dn = np.zeros(len(m)), np.zeros(len(m))
    peak = np.zeros(len(m))
    bar, c = np.log(2 / alpha), 0.5
    out_lo, out_hi = np.empty(n), np.empty(n)
    mean, var = 0.5, 0.25
    for t in range(n):
        lam = min(np.sqrt(2 * np.log(2 / alpha) / (max(var, 1e-6) * (t + 1) * np.log(t + 2))), 1e6)
        d = y[t] - m
        logk_up += np.log1p(np.clip(np.minimum(lam, c / m) * d, -0.999999, None))
        logk_dn += np.log1p(np.clip(-np.minimum(lam, c / (1 - m)) * d, -0.999999, None))
        peak = np.maximum(peak, np.maximum(logk_up, logk_dn))
        alive = np.flatnonzero(peak < bar)
        if len(alive):
            first, last = alive[0], alive[-1]
            out_lo[t], out_hi[t] = (m[first - 1] if first else 0.0), (m[last + 1] if last + 1 < len(m) else 1.0)
        else:
            out_lo[t] = out_hi[t] = float(np.mean(y[: t + 1]))
        mean = (0.5 + y[: t + 1].sum()) / (t + 2)
        var = (0.25 + ((y[: t + 1] - mean) ** 2).sum()) / (t + 2)
    out_lo, out_hi = np.maximum.accumulate(out_lo), np.minimum.accumulate(out_hi)
    return lo + (hi - lo) * out_lo, lo + (hi - lo) * np.maximum(out_hi, out_lo)
