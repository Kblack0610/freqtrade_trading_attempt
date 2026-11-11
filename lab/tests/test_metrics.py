import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from lab import metrics
from lab.util import utc


FEE = 0.002  # round trip


def trades(*rows) -> pd.DataFrame:
    return pd.DataFrame([{"open_date": utc(o), "close_date": utc(c), "open_rate": 100.0,
                          "close_rate": 100.0 * (1 + p / 100 + FEE), "min_rate": 100.0 + min(p, 0), "profit_abs": p, "profit_ratio": p / 100}
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
    idx = metrics.block_sample(np.arange(7), 3, 50, np.random.default_rng(1), 9)
    assert idx.shape == (50, 9) and idx.min() >= 0 and idx.max() < 7
    assert all((idx[:, 1] - idx[:, 0]) % 7 == 1)


@pytest.mark.parametrize("n, block, length", [(400, 10, None), (200, 10, 90), (7, 3, 9), (2, 5, 12), (30, 1, None)])
def test_block_bootstrap_draws_what_the_index_form_drew(n, block, length):
    """The windowed draw is the old modulo-index one, value for value, so every stored interval stays as it was."""
    x = np.random.default_rng(3).normal(0, 1, n)
    rng = np.random.default_rng(9)
    starts = rng.integers(0, n, size=(50, -(-(length or n) // block)))
    want = x[((starts[:, :, None] + np.arange(block)) % n).reshape(50, -1)[:, :length or n]]
    assert np.array_equal(metrics.block_sample(x, block, 50, np.random.default_rng(9), length), want)


def test_intervals_and_monte_carlo_repeat_for_a_seed():
    r = np.random.default_rng(3).normal(0.001, 0.02, 400)
    assert metrics.sharpe_ci(r, 7) == metrics.sharpe_ci(r, 7) != metrics.sharpe_ci(r, 8)
    lo, hi = metrics.sharpe_ci(r, 7)
    assert lo < metrics.sharpe(r) < hi
    a, b = metrics.monte_carlo(r, 90, 11), metrics.monte_carlo(r, 90, 11)
    assert a == b and a != metrics.monte_carlo(r, 90, 12)
    assert a["p10"] < a["p50"] < a["p90"] and 0 < a["prob_loss"] < 1 and a["exp_max_drawdown"] > 0
    assert metrics.mean_ci(np.array([1.0]), 1) == (None, None)


def test_minimum_backtest_years_follows_the_bailey_bound():
    assert math.isclose(metrics.min_backtest_years(1.0, 10), 2 * math.log(10))
    assert math.isclose(metrics.min_backtest_years(0.5, 10), 4 * metrics.min_backtest_years(1.0, 10))
    assert metrics.min_backtest_years(1.0, 1) == 0.0
    assert metrics.min_backtest_years(0.0, 10) is None and metrics.min_backtest_years(-1.0, 10) is None
    assert metrics.min_backtest_years(None, 10) is None


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


def test_worst_open_sees_a_loss_that_closed_as_a_win():
    d = pd.Timestamp("2026-01-01", tz="UTC")
    t = trades((d, d, 1.0), (d, d, -2.0)).assign(min_rate=[70.0, 98.0])
    assert t["profit_abs"].iloc[0] > 0 and math.isclose(metrics.worst_open(t), -0.30)
    assert metrics.worst_open(t.iloc[0:0]) is None
    assert metrics.worst_open(t.assign(min_rate=None)) is None


def test_capital_exposure_weights_each_trade_by_its_share_and_time():
    start, end = pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-11", tz="UTC")
    t = trades((start, start + pd.Timedelta(days=5), 1.0), (start, start + pd.Timedelta(days=5), 1.0),
               (start - pd.Timedelta(days=5), start + pd.Timedelta(days=1), 1.0)).assign(share=[0.3, 0.3, 0.5])
    assert math.isclose(metrics.capital_exposure(t, start, end), (0.3 * 5 + 0.3 * 5 + 0.5 * 1) / 10)
    assert metrics.exposure(t, start, end) == 0.5
    assert metrics.capital_exposure(t.iloc[0:0], start, end) == 0.0


def hold_series(n=400, seed=1):
    days = pd.date_range("2025-01-01", periods=n, freq="D").date
    btc = np.random.default_rng(seed).normal(0.002, 0.03, n)
    close = pd.Series(100.0 * np.cumprod(1 + btc), index=days)
    return days, btc, close


def test_riding_the_market_is_no_excess_over_hold():
    days, btc, close = hold_series()
    daily = pd.DataFrame({"day": days, "ret": 0.5 * btc})
    x, lo, hi = metrics.hold_excess(daily, close, 0.5, 3)
    assert abs(x) < 1e-9 and abs(lo) < 1e-9 and abs(hi) < 1e-9
    assert metrics.hold_excess(daily, close, 0.5, 3) == (x, lo, hi)


def test_a_real_edge_clears_hold_and_short_history_gives_nothing():
    days, btc, close = hold_series()
    daily = pd.DataFrame({"day": days, "ret": 0.5 * btc + 0.003})
    x, lo, hi = metrics.hold_excess(daily, close, 0.5, 3)
    assert math.isclose(x, 0.003 * 365) and lo <= x <= hi and lo > 0
    assert metrics.hold_excess(daily.tail(10), close, 0.5, 3) == (None, None, None)
    assert metrics.hold_excess(daily, pd.Series(dtype=float), 0.5, 3) == (None, None, None)


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
    r = metrics.regimes(close, 0.05, 30)
    assert r["trend"].iloc[:199].isna().all() and (r["trend"].iloc[199:] == "up").all()
    assert set(r["vol_bucket"].dropna()) <= {"low", "mid", "high"}
    assert r["vol_bucket"].iloc[:59].isna().all() and r["vol_bucket"].iloc[59:].notna().all()


def test_a_regime_label_never_changes_when_later_days_arrive():
    rng = np.random.default_rng(3)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 700))), index=pd.date_range("2022-01-01", periods=700))
    full = metrics.regimes(close, 0.05, 90).set_index("day")
    for n in (300, 450, 600):
        cut = metrics.regimes(close.iloc[:n], 0.05, 90).set_index("day")
        pd.testing.assert_frame_equal(cut[["trend", "vol_bucket"]], full.loc[cut.index, ["trend", "vol_bucket"]])
    # the whole-history terciles this replaced do change: the first label moves once a calm year is appended
    calm = pd.concat([close, pd.Series(close.iloc[-1] * np.exp(np.cumsum(rng.normal(0, 0.001, 700))),
                                       index=pd.date_range(close.index[-1] + pd.Timedelta(days=1), periods=700))])
    vol = lambda c: np.log(c).diff().rolling(30).std()
    assert vol(close).quantile(2 / 3) != vol(calm).quantile(2 / 3)


def test_drift_matches_entries_within_a_candle():
    t = utc
    live = pd.DataFrame({"pair": ["BTC/USD", "BTC/USD", "ETH/USD"],
                         "open_date": [t("2026-11-01 10:02"), t("2026-11-02 09:00"), t("2026-11-02 12:00")],
                         "is_open": [False, False, True], "profit_ratio": [0.01, -0.02, 0.0], "profit_abs": [3.0, -6.0, 0.0]})
    bt = pd.DataFrame({"pair": ["BTC/USD", "ETH/USD", "ETH/USD"],
                       "open_date": [t("2026-11-01 10:00"), t("2026-11-02 12:00"), t("2026-11-03 00:00")],
                       "exit_reason": ["roi", "force_exit", "roi"], "profit_ratio": [0.015, 0.01, 0.02],
                       "profit_abs": [4.5, 3.0, 6.0]})
    d = metrics.drift(live, bt, pd.Timedelta("5m"))
    assert (d["live_entries"], d["bt_entries"], d["matched"], d["live_closed"], d["bt_closed"]) == (3, 3, 2, 2, 2)
    assert d["gap_trades"] == 1 and math.isclose(d["gap"], -0.005) and d["gaps"] == pytest.approx([-0.005])
    assert math.isclose(d["live_expectancy"], -0.005) and math.isclose(d["bt_profit"], 10.5)
    assert metrics.drift(live.iloc[0:0], bt, pd.Timedelta("5m"))["matched"] == 0


def test_seeds_fit_the_hyperopt_samplers():
    seeds = [metrics.seed_of("Strategy001", "binanceus", dt.date(2026, m, 1)) for m in range(1, 13)]
    assert all(0 <= s < 2 ** 32 for s in seeds)
    np.random.RandomState(max(seeds))  # optuna's samplers seed numpy's RandomState, which refuses 2**32 and up


def test_effective_trials_counts_independent_series_once_each_and_copies_once():
    rng = np.random.default_rng(1)
    x = pd.DataFrame(rng.normal(0, 0.01, size=(2000, 6)))
    assert 5.5 < metrics.effective_trials(x) <= 6.0
    same = pd.DataFrame({i: x[0] for i in range(6)})
    assert metrics.effective_trials(same) == pytest.approx(1.0)
    # three copies of one plus three independent: eigenvalues 3, 1, 1, 1, so 6^2 / 12
    mixed = pd.concat([same.iloc[:, :3], x.iloc[:, 3:]], axis=1, ignore_index=True)
    assert metrics.effective_trials(mixed) == pytest.approx(3.0, abs=0.15)
    assert metrics.effective_trials(x.iloc[:, :1]) == 1.0


def test_pbo_is_low_with_one_truly_better_strategy_and_near_half_on_noise():
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.01, size=(800, 20))
    r = metrics.pbo(noise)
    assert r["splits"] == 12870 and 0.3 < r["pbo"] < 0.7
    planted = noise.copy()
    planted[:, 3] += 0.004
    assert metrics.pbo(planted)["pbo"] < 0.05
    assert metrics.pbo(noise[:20]) is None and metrics.pbo(noise[:, :1]) is None


def test_trades_needed_matches_the_closed_form():
    x = np.array([0.03, -0.01] * 50)  # mean 0.01, sd about 0.0201
    expect = math.ceil((1.645 * x.std(ddof=1) / x.mean()) ** 2) - len(x)
    assert metrics.trades_needed(x) == max(0, expect)
    small = np.array([0.05, -0.04, 0.03, -0.02])
    assert metrics.trades_needed(small) == math.ceil((1.645 * small.std(ddof=1) / small.mean()) ** 2) - 4
    assert metrics.trades_needed(np.array([-0.01, 0.005])) is None


def test_diff_ci_straddles_zero_for_one_sample_and_clears_it_for_a_shifted_one():
    rng = np.random.default_rng(3)
    x = rng.normal(0.001, 0.01, 400)
    lo, hi = metrics.diff_ci(x, x, 7)
    assert lo < 0 < hi
    lo, _ = metrics.diff_ci(x + 0.01, x, 7)
    assert lo > 0
    assert metrics.diff_ci(x, np.array([1.0]), 7) == (None, None)


def test_confidence_sequence_keeps_its_level_under_optional_stopping():
    """The negative control: a fixed-sample t test read after every trade rejects a true mean far above alpha; the
    sequence, read just as often, excludes the true mean in at most alpha of runs."""
    from scipy.stats import t as student
    rng, alpha, n, runs = np.random.default_rng(7), 0.1, 150, 300
    seq_miss = t_miss = 0
    for _ in range(runs):
        x = np.clip(rng.normal(0.01, 0.04, n), -0.3, 0.3)
        lo, hi = metrics.confidence_sequence(x, -0.3, 0.3, alpha, grid=100)
        seq_miss += bool(((lo > 0.01) | (hi < 0.01)).any())
        k = np.arange(2, n + 1)
        mean = np.cumsum(x)[1:] / k
        sd = np.array([x[:i].std(ddof=1) for i in k])
        half = student.ppf(1 - alpha / 2, k - 1) * sd / np.sqrt(k)
        t_miss += bool(((mean - half > 0.01) | (mean + half < 0.01)).any())
    assert seq_miss / runs <= alpha
    assert t_miss / runs > 2 * alpha


def test_confidence_sequence_separates_a_real_edge_and_never_widens():
    x = np.random.default_rng(1).normal(0.03, 0.02, 200)
    lo, hi = metrics.confidence_sequence(x, -0.3, 0.3, 0.05, grid=200)
    assert lo[-1] > 0 and lo[-1] <= x.mean() <= hi[-1]
    assert (np.diff(lo) >= 0).all() and (np.diff(hi) <= 0).all()
    assert metrics.confidence_sequence([], -1, 1, 0.05)[0].size == 0
    bad_lo, bad_hi = metrics.confidence_sequence(np.full(100, -0.05), -0.3, 0.3, 0.05, grid=200)
    assert bad_hi[-1] < 0
