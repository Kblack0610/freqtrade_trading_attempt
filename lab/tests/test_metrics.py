import datetime as dt
import math

import numpy as np
import pandas as pd

import metrics


FEE = 0.002  # round trip


def trades(*rows) -> pd.DataFrame:
    return pd.DataFrame([{"open_date": metrics.utc(o), "close_date": metrics.utc(c), "open_rate": 100.0,
                          "close_rate": 100.0 * (1 + p / 100 + FEE), "profit_abs": p, "profit_ratio": p / 100}
                         for o, c, p in rows])


def test_daily_returns_compound_on_the_running_balance():
    d = metrics.daily_returns(trades(("2026-01-01 01:00", "2026-01-01 05:00", 10.0),
                                     ("2026-01-02 09:00", "2026-01-03 02:00", -20.0)),
                              "2026-01-01", "2026-01-05", 1000.0)
    assert list(d["day"]) == [dt.date(2026, 1, d_) for d_ in (1, 2, 3, 4)]
    assert d["ret"].tolist() == [0.01, 0.0, -20 / 1010, 0.0]
    assert d["trades"].tolist() == [1, 0, 1, 0]


def test_daily_returns_of_no_trades_are_zeros():
    d = metrics.daily_returns(trades().reindex(columns=["open_date", "close_date", "profit_abs"]),
                              "2026-01-01", "2026-01-04", 1000.0)
    assert d["ret"].tolist() == [0.0, 0.0, 0.0]


def test_max_drawdown_and_sharpe():
    assert math.isclose(metrics.max_drawdown(np.array([0.1, -0.5, 0.2])), 0.5)
    assert metrics.max_drawdown(np.array([0.01, 0.02])) == 0.0
    assert metrics.sharpe(np.zeros(10)) is None
    assert metrics.sharpe(np.array([0.01, 0.03])) > 0


def test_exposure_merges_overlapping_trades():
    t = trades(("2026-01-01 00:00", "2026-01-01 12:00", 1.0), ("2026-01-01 06:00", "2026-01-01 18:00", 1.0),
               ("2026-01-02 00:00", "2026-01-02 06:00", 1.0))
    e = metrics.exposure(t, pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-03", tz="UTC"))
    assert math.isclose(e, 24 / 48)


def test_block_bootstrap_keeps_runs_and_wraps():
    idx = metrics.block_indices(7, 3, 50, np.random.default_rng(1), 9)
    assert idx.shape == (50, 9) and idx.min() >= 0 and idx.max() < 7
    assert all((idx[:, 1] - idx[:, 0]) % 7 == 1)


def test_intervals_and_monte_carlo_repeat_for_a_seed():
    r = np.random.default_rng(3).normal(0.001, 0.02, 400)
    assert metrics.sharpe_ci(r, 7) == metrics.sharpe_ci(r, 7) != metrics.sharpe_ci(r, 8)
    lo, hi = metrics.sharpe_ci(r, 7)
    assert lo < metrics.sharpe(r) < hi
    a, b = metrics.monte_carlo(r, 90, 11), metrics.monte_carlo(r, 90, 11)
    assert a == b and a != metrics.monte_carlo(r, 90, 12)
    assert a["p10"] < a["p50"] < a["p90"] and 0 < a["prob_loss"] < 1 and a["exp_max_drawdown"] > 0
    assert metrics.mean_ci(np.array([1.0]), 1) == (None, None)


def test_deflated_sharpe_falls_as_trials_grow():
    sr, n = 0.08, 365
    alone = metrics.deflated_sharpe(sr, n, 0.0, 3.0, 1)
    crowd = metrics.deflated_sharpe(sr, n, 0.0, 3.0, 61)
    assert alone > 0.9 and crowd < alone
    assert metrics.deflated_sharpe(None, n, 0, 3, 5) is None


def test_deflated_sharpe_clears_a_real_edge_and_not_luck():
    n, k = 365, 89
    luck_bar = 2.5 / math.sqrt(n - 1)  # about the expected best of 89 zero-Sharpe trials over a year
    assert metrics.deflated_sharpe(0.25, n, 0.0, 3.0, k) > 0.95  # annualized 4.8
    assert metrics.deflated_sharpe(luck_bar, n, 0.0, 3.0, k) < 0.6
    assert metrics.deflated_sharpe(0.05, n, 0.0, 3.0, k) < 0.1  # annualized 1.0 is luck among 89


def test_window_metrics_on_a_known_series():
    days = pd.date_range("2026-01-01", periods=60, freq="D", tz="UTC")
    t = trades(*[(d, d + pd.Timedelta(hours=6), 10.0 if i % 3 else -5.0) for i, d in enumerate(days[::2])])
    daily = metrics.daily_returns(t, days[0], days[-1] + pd.Timedelta(days=1), 1000.0)
    m = metrics.window_metrics(daily, t, 5)
    assert m["days"] == 60 and m["trades"] == 30 and m["total_return"] > 0
    assert m["profit_factor"] == 200 / 50 and math.isclose(m["winrate"], 20 / 30)
    assert m["expectancy_lo"] < m["expectancy"] < m["expectancy_hi"]
    assert math.isclose(m["gross_expectancy"], m["expectancy"] + FEE) and math.isclose(m["fee_per_trade"], FEE)
    assert m["gross_expectancy_lo"] < m["gross_expectancy"] < m["gross_expectancy_hi"]
    assert math.isclose(m["exposure"], 30 * 6 / (60 * 24))
    assert m == metrics.window_metrics(daily, t, 5)


def test_regimes_need_200_days_for_a_trend():
    close = pd.Series(np.linspace(100, 300, 260), index=pd.date_range("2025-01-01", periods=260).date)
    r = metrics.regimes(close)
    assert r["trend"].iloc[:199].isna().all() and (r["trend"].iloc[199:] == "bull").all()
    assert set(r["vol_bucket"].dropna()) <= {"low", "mid", "high"}


def test_drift_matches_entries_within_a_candle():
    t = metrics.utc
    live = pd.DataFrame({"pair": ["BTC/USD", "BTC/USD", "ETH/USD"],
                         "open_date": [t("2026-11-01 10:02"), t("2026-11-02 09:00"), t("2026-11-02 12:00")],
                         "is_open": [False, False, True], "profit_ratio": [0.01, -0.02, 0.0], "profit_abs": [3.0, -6.0, 0.0]})
    bt = pd.DataFrame({"pair": ["BTC/USD", "ETH/USD", "ETH/USD"],
                       "open_date": [t("2026-11-01 10:00"), t("2026-11-02 12:00"), t("2026-11-03 00:00")],
                       "exit_reason": ["roi", "force_exit", "roi"], "profit_ratio": [0.015, 0.01, 0.02],
                       "profit_abs": [4.5, 3.0, 6.0]})
    d = metrics.drift(live, bt, pd.Timedelta("5m"))
    assert (d["live_entries"], d["bt_entries"], d["matched"], d["live_closed"], d["bt_closed"]) == (3, 3, 2, 2, 2)
    assert d["gap_trades"] == 1 and math.isclose(d["gap"], -0.005)
    assert math.isclose(d["live_expectancy"], -0.005) and math.isclose(d["bt_profit"], 10.5)
    assert metrics.drift(live.iloc[0:0], bt, pd.Timedelta("5m"))["matched"] == 0


def test_seeds_fit_the_hyperopt_samplers():
    seeds = [metrics.seed_of("Strategy001", "binanceus", dt.date(2026, m, 1)) for m in range(1, 13)]
    assert all(0 <= s < 2 ** 32 for s in seeds)
    np.random.RandomState(max(seeds))  # optuna's samplers seed numpy's RandomState, which refuses 2**32 and up
