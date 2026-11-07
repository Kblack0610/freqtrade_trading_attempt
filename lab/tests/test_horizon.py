import datetime as dt

import numpy as np
import pandas as pd

from lab import horizon, suggest

CFG = suggest.load_config()


def market(days=500):
    index = pd.date_range("2023-01-01", periods=days, freq="D")
    return pd.Series(100 * (1 + 0.002) ** np.arange(days), index=index)


def test_the_filter_holds_only_after_a_close_above_the_mean():
    close = market()
    p = horizon.plans(close, 200)
    assert p["trend"].index.min() > close.index[199]
    assert (p["trend"].dropna() == p["hold"].reindex(p["trend"].dropna().index)).all()


def test_a_steady_winner_beats_both_plans_and_a_loser_loses():
    close = market()
    start, end = dt.date(2023, 12, 1), dt.date(2024, 5, 1)
    days = pd.date_range(start, end - dt.timedelta(days=1), freq="D")
    rng = np.random.default_rng(2)
    frames = {(n, "x", "base"): pd.DataFrame({"ret": m + rng.normal(0, 0.0005, len(days)), "trades": 1}, index=days)
              for n, m in (("W", 0.01), ("L", -0.01))}
    out = horizon.build(close, {("W", "x"): (start, end), ("L", "x"): (start, end)}, frames, CFG)
    by = {(r["strategy"], r["plan"]): r for r in out}
    assert by["W", "hold"]["verdict"] == "beats" and by["W", "trend"]["verdict"] == "beats"
    assert by["L", "hold"]["verdict"] == "loses" and by["L", "hold"]["days"] == len(days)


def test_too_few_days_gives_no_row():
    close = market()
    start, end = dt.date(2023, 12, 1), dt.date(2023, 12, 11)
    days = pd.date_range(start, end - dt.timedelta(days=1), freq="D")
    out = horizon.build(close, {("W", "x"): (start, end)}, {("W", "x", "base"): pd.DataFrame({"ret": 0.01, "trades": 1}, index=days)}, CFG)
    assert out == []
