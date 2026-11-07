#!/usr/bin/env python3
"""The out-of-sample result per market climate: what each strategy, and BTC held, earned on the days of each climate.

A climate is the BTC trend bucket (up, down, flat; lab/metrics.trend_buckets) and the volatility tercile of
`regimes`, both read at the close of the day before, so a day's label never uses that day's return. A strategy's
days are the calendar days of its walk-forward test months, zero where it did not trade, from `walkforward_daily`
(trade profit ratios summed per close day; lab/walkforward.py writes them, trades across fold ends already purged).
annual is the mean daily value x 365; lo and hi are its block-bootstrap interval. A cell clears when lo is above zero.
The interval is per cell and not corrected for how many cells and strategies were looked at: `trials` is the
register's size, the bar that the deflated Sharpe on /confidence applies. Advice for a human; nothing here trades.

Run alone with `python -m lab.scorecard`; lab/analyze.py and lab/walkforward.py run it after the book.
"""

import sys

import numpy as np
import pandas as pd

from lab import metrics, suggest
from lab.db import connect

COLS = ["kind", "name", "exchange", "variant", "cell", "days", "trades", "annual", "lo", "hi", "trials"]
SPANS = "SELECT strategy, exchange, min(fold_start), max(test_end) FROM walkforward WHERE ok GROUP BY 1, 2"


def labels(close: pd.Series, vol_bucket: pd.Series, band: float) -> pd.Series:
    """trend/vol per day, each from the day before; a day without both has no label."""
    trend = metrics.trend_buckets(close, band)
    both = pd.concat([trend.astype(object), vol_bucket.reindex(trend.index).astype(object)], axis=1).dropna()
    return (both.iloc[:, 0] + "/" + both.iloc[:, 1]).reindex(trend.index).shift(1).dropna()


def cells(daily: pd.Series, trades: pd.Series, label: pd.Series, key: tuple, cfg: dict, trials: int) -> list[dict]:
    """One row per climate present in `daily` (index: day), the interval once it has min_days days."""
    c, out = cfg["scorecard"], []
    both = pd.concat([daily.rename("ret"), trades.rename("n"), label.rename("cell")], axis=1, join="inner")
    for cell, g in both.groupby("cell"):
        x = g["ret"].to_numpy(dtype=float)
        lo, hi = metrics.block_mean_ci(x, metrics.seed_of(*key, cell), block=c["block"], level=c["level"]) \
            if len(x) >= c["min_days"] else (None, None)
        out.append({"kind": key[0], "name": key[1], "exchange": key[2], "variant": key[3], "cell": cell, "days": len(x),
                    "trades": int(g["n"].sum()), "annual": float(x.mean() * metrics.YEAR),
                    "lo": None if lo is None else lo * metrics.YEAR, "hi": None if hi is None else hi * metrics.YEAR,
                    "trials": trials})
    return out


def build(close: pd.Series, vol_bucket: pd.Series, spans: dict, daily: dict, cfg: dict, trials: int) -> list[dict]:
    """spans: (strategy, exchange) -> (first day, last day exclusive); daily: (strategy, exchange, variant) -> frame
    of ret and trades by day."""
    label = labels(close, vol_bucket, cfg["confidence"]["trend_band"])
    out = []
    ret = close.sort_index().pct_change().dropna()
    out += cells(ret, pd.Series(0, index=ret.index), label, ("benchmark", "hold BTC", "", ""), cfg, trials)
    for (s, e, v), frame in sorted(daily.items()):
        if (s, e) not in spans:
            continue
        start, end = spans[(s, e)]
        days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) - pd.Timedelta(days=1), freq="D")
        f = frame.reindex(days).fillna(0.0)
        out += cells(f["ret"], f["trades"], label, ("strategy", s, e, v), cfg, trials)
    return out


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    regimes = conn.execute("SELECT day, btc_close, vol_bucket FROM regimes ORDER BY day").fetchall()
    index = pd.to_datetime([r[0] for r in regimes])
    close = pd.Series([r[1] for r in regimes], index=index, dtype=float)
    vol = pd.Series([r[2] for r in regimes], index=index, dtype=object)
    spans = {(s, e): (a, b) for s, e, a, b in conn.execute(SPANS)}
    daily: dict = {}
    for s, e, v, day, ret, n in conn.execute("SELECT strategy, exchange, variant, day, ret, trades FROM walkforward_daily "
                                             "ORDER BY 1, 2, 3, 4"):
        daily.setdefault((s, e, v), []).append((pd.Timestamp(day), ret, n))
    frames = {k: pd.DataFrame(v, columns=["day", "ret", "trades"]).set_index("day") for k, v in daily.items()}
    trials = conn.execute("SELECT count(*) FROM trials").fetchone()[0]
    out = build(close, vol, spans, frames, cfg, trials)
    with conn.transaction():
        conn.execute("DELETE FROM climate_scorecard")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO climate_scorecard ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
    return {"scorecard": len(out)}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"scorecard: {run(conn)}", flush=True)
    sys.exit(0)
