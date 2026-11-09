#!/usr/bin/env python3
"""Long term against short term: does an active strategy beat holding BTC, or holding it only above its long mean?

The plain plans (hold, monthly DCA, long-mean filter) are priced over whole windows by lab/book.py. Here each
strategy's own out-of-sample days (the calendar days of its walk-forward test months, zero where it did not trade,
from `walkforward_daily`) are set against hold BTC and the long-mean filter on those same days. diff_lo and diff_hi
bound the mean daily difference x 365 by block bootstrap. beats needs the low end above zero, loses the high end
below it, anything else is open and the passive plan stays the default. Advice for a human: buying a core hold is
the human's own purchase and nothing here trades. Intervals are per strategy and not corrected for how many were looked at.

Run alone with `python -m lab.horizon`; lab/analyze.py and lab/walkforward.py run it after the scorecard.
"""

import sys

import numpy as np
import pandas as pd

from lab import metrics, scorecard, suggest
from lab.db import connect

COLS = ["strategy", "exchange", "variant", "plan", "days", "strat_annual", "plan_annual", "diff_lo", "diff_hi", "verdict"]


def plans(close: pd.Series, trend_days: int) -> dict[str, pd.Series]:
    """Daily returns of each passive plan; the filter holds on a day only if yesterday closed above its mean."""
    ret = close.sort_index().pct_change()
    held = (close > close.rolling(trend_days).mean()).shift(1, fill_value=False)
    return {"hold": ret, "trend": (ret * held)[close.rolling(trend_days).mean().shift(1).notna()]}


def verdict(lo, hi) -> str:
    return "open" if lo is None else "beats" if lo > 0 else "loses" if hi < 0 else "open"


def build(close: pd.Series, spans: dict, daily: dict, cfg: dict) -> list[dict]:
    """spans and daily as in scorecard.build."""
    c = cfg["horizon"]
    passive = plans(close, cfg["book"]["trend_days"])
    out = []
    for (s, e, v), frame in sorted(daily.items()):
        if (s, e) not in spans:
            continue
        start, end = spans[(s, e)]
        days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) - pd.Timedelta(days=1), freq="D")
        mine = frame["ret"].reindex(days).fillna(0.0)
        for plan, ret in passive.items():
            both = pd.concat([mine.rename("a"), ret.rename("b")], axis=1, join="inner").dropna()
            if len(both) < c["min_days"]:
                continue
            diff = (both["a"] - both["b"]).to_numpy(dtype=float)
            lo, hi = metrics.block_mean_ci(diff, metrics.seed_of(s, e, v, plan), block=c["block"], level=c["level"])
            lo, hi = (None if lo is None else lo * metrics.YEAR), (None if hi is None else hi * metrics.YEAR)
            out.append({"strategy": s, "exchange": e, "variant": v, "plan": plan, "days": len(both),
                        "strat_annual": float(both["a"].mean() * metrics.YEAR), "plan_annual": float(both["b"].mean() * metrics.YEAR),
                        "diff_lo": lo, "diff_hi": hi, "verdict": verdict(lo, hi)})
    return out


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    regimes = conn.execute("SELECT day, btc_close FROM regimes ORDER BY day").fetchall()
    close = pd.Series([r[1] for r in regimes], index=pd.to_datetime([r[0] for r in regimes]), dtype=float)
    box = (cfg["lockbox"]["start"],)
    spans = {(s, e): (a, b) for s, e, a, b in conn.execute(scorecard.SPANS, box)}
    daily: dict = {}
    for s, e, v, day, ret, n in conn.execute(scorecard.DAILY, box):
        daily.setdefault((s, e, v), []).append((pd.Timestamp(day), ret, n))
    frames = {k: pd.DataFrame(v, columns=["day", "ret", "trades"]).set_index("day") for k, v in daily.items()}
    out = build(close, spans, frames, cfg)
    with conn.transaction():
        conn.execute("DELETE FROM horizon_verdict")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO horizon_verdict ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
    return {"horizon": len(out)}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"horizon: {run(conn)}", flush=True)
    sys.exit(0)
