#!/usr/bin/env python3
"""Volatility forecast: does HAR on realized variance beat the plain forecasts on months it never saw?

Per venue and whitelisted pair, from candles of [volatility] timeframe:
- realized variance per UTC day is the sum of squared candle log returns, scaled up for a few missing candles;
- each day's variance is forecast from the days before it three ways: HAR (Corsi 2009: a regression of log
  variance on its mean over the last day, week and month, refit every month on the days before that month only,
  with a lognormal bias correction), the mean of the last [volatility] baseline_days, and an EWMA;
- each forecast is scored by QLIKE (Patton 2011: rv / f - ln(rv / f) - 1, the loss that ranks variance forecasts
  correctly when the proxy is noisy), on the same days for all three, every whole month before the lockbox.
HAR wins on a venue when, against each baseline, the high end of a block-bootstrap interval on the daily QLIKE
difference (HAR minus baseline, averaged over pairs) is below zero. Only then may sizing use it; the verdict is
written to `vol_verdict` and shown on /book either way.

lab/analyze.py runs it after the derived tables.
"""

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from lab import metrics

MODELS = ("har", "mean", "ewma")
SCORE_COLS = ["exchange", "pair", "model", "fold_start", "days", "qlike"]
VERDICT_COLS = ["exchange", "baseline", "pairs", "days", "har_qlike", "baseline_qlike", "diff_lo", "diff_hi", "har_wins"]


def realized_variance(candles: pd.DataFrame, minutes: int, min_share: float) -> pd.Series:
    """Daily realized variance from candles (date, close), indexed by UTC day. A return counts only between
    adjacent candles; a day with fewer than min_share of its returns is dropped, the rest are scaled to a full day."""
    c = candles.sort_values("date")
    date = pd.to_datetime(c["date"], utc=True)
    step = pd.Timedelta(minutes=minutes)
    ret = np.log(c["close"].astype(float).to_numpy())
    ret = pd.Series(np.diff(ret, prepend=np.nan), index=date).where(date.diff().to_numpy() == step)
    per_day = 24 * 60 // minutes
    g = (ret ** 2).groupby(date.dt.floor("D").to_numpy())
    rv = g.sum(min_count=1) * per_day / g.count()
    rv = rv[(g.count() >= min_share * per_day) & (rv > 0)]
    return rv.set_axis(pd.DatetimeIndex(rv.index).date)


def forecasts(rv: pd.Series, months: list[dt.date], cfg: dict) -> pd.DataFrame:
    """Each scored day of the months given: the realized variance and the three forecasts made the day before.
    HAR for a month is fit only on days before that month; a month with too few such days is not scored."""
    c = cfg["volatility"]
    if rv.empty:
        return pd.DataFrame(columns=["day", "fold_start", "rv", *MODELS])
    days = pd.date_range(min(rv.index), max(rv.index), freq="D").date
    rv = rv.reindex(days)
    x = pd.concat({k: np.log(rv.rolling(k, min_periods=k).mean()) for k in c["har_days"]}, axis=1).shift(1)
    base = pd.DataFrame({"mean": rv.rolling(c["baseline_days"], min_periods=c["baseline_days"]).mean().shift(1),
                         "ewma": rv.ewm(alpha=1 - c["ewma_lambda"], ignore_na=True).mean().shift(1)
                         .where(rv.rolling(c["baseline_days"]).count().shift(1) >= c["baseline_days"])})
    y, out = np.log(rv), []
    usable = x.notna().all(axis=1) & y.notna()
    for start in months:
        end = next_month(start)
        train = usable & (x.index < start)
        if train.sum() < c["min_train_days"]:
            continue
        a = np.column_stack([np.ones(train.sum()), x[train].to_numpy()])
        beta, *_ = np.linalg.lstsq(a, y[train].to_numpy(), rcond=None)
        s2 = float(np.var(y[train].to_numpy() - a @ beta, ddof=a.shape[1]))
        test = usable & (x.index >= start) & (x.index < end) & base.notna().all(axis=1)
        if not test.any():
            continue
        har = np.exp(np.column_stack([np.ones(test.sum()), x[test].to_numpy()]) @ beta + s2 / 2)
        out.append(pd.DataFrame({"day": x.index[test], "fold_start": start, "rv": rv[test].to_numpy(), "har": har,
                                 "mean": base["mean"][test].to_numpy(), "ewma": base["ewma"][test].to_numpy()}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["day", "fold_start", "rv", *MODELS])


def qlike(rv: np.ndarray, f: np.ndarray) -> np.ndarray:
    r = np.asarray(rv, dtype=float) / np.asarray(f, dtype=float)
    return r - np.log(r) - 1.0


def next_month(day: dt.date) -> dt.date:
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def months_before(first: dt.date, lockbox: dt.date) -> list[dt.date]:
    """Every month start from the one after `first` up to the lockbox, which stays unscored."""
    out, m = [], next_month(first)
    while m < lockbox:
        out.append(m)
        m = next_month(m)
    return out


def score(exchange: str, series: dict[str, pd.Series], cfg: dict) -> tuple[list[dict], list[dict]]:
    """series: pair -> daily realized variance. The per-pair, per-model, per-month QLIKE rows, and the verdict rows."""
    c, lockbox = cfg["volatility"], cfg["lockbox"]["start"]
    scores, daily = [], []
    for pair, rv in sorted(series.items()):
        if rv.empty:
            continue
        f = forecasts(rv, months_before(min(rv.index), lockbox), cfg)
        if f.empty:
            continue
        loss = pd.DataFrame({m: qlike(f["rv"], f[m]) for m in MODELS}).assign(day=f["day"], fold_start=f["fold_start"])
        for (start, model), q in loss.melt(id_vars=["day", "fold_start"], var_name="model").groupby(["fold_start", "model"]):
            scores.append({"exchange": exchange, "pair": pair, "model": model, "fold_start": start, "days": len(q),
                           "qlike": float(q["value"].mean())})
        daily.append(loss.assign(pair=pair))
    if not daily:
        return scores, []
    by_day = pd.concat(daily).groupby("day")[list(MODELS)].mean().sort_index()
    verdict = []
    for b in MODELS[1:]:
        lo, hi = metrics.block_mean_ci((by_day["har"] - by_day[b]).to_numpy(), metrics.seed_of("volatility", exchange, b),
                                       block=c["block_days"], level=c["level"])
        verdict.append({"exchange": exchange, "baseline": b, "pairs": len(daily), "days": len(by_day),
                        "har_qlike": float(by_day["har"].mean()), "baseline_qlike": float(by_day[b].mean()),
                        "diff_lo": lo, "diff_hi": hi, "har_wins": hi is not None and hi < 0})
    return scores, verdict


def run(conn, files: dict[str, dict[str, Path]], cfg: dict) -> dict:
    """files: exchange -> pair -> its candle file at [volatility] timeframe. Rebuilds vol_scores and vol_verdict."""
    c = cfg["volatility"]
    minutes = int(pd.Timedelta(c["timeframe"]).total_seconds() // 60)
    scores, verdict = [], []
    for ex, pairs in sorted(files.items()):
        series = {p: realized_variance(pd.read_feather(f, columns=["date", "close"]), minutes, c["min_day_share"])
                  for p, f in pairs.items() if f.exists()}
        s, v = score(ex, series, cfg)
        scores += s
        verdict += v
    with conn.transaction():
        conn.execute("DELETE FROM vol_scores")
        conn.execute("DELETE FROM vol_verdict")
        with conn.cursor() as cur:
            for table, cols, rows in (("vol_scores", SCORE_COLS, scores), ("vol_verdict", VERDICT_COLS, verdict)):
                cur.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(f'%({k})s' for k in cols)})",
                                rows)
    wins = sorted({v["exchange"] for v in verdict} - {v["exchange"] for v in verdict if not v["har_wins"]})
    return {"vol_scores": len(scores), "vol_har_wins": wins}
