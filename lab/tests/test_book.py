import datetime as dt

import numpy as np
import pandas as pd
import pytest

import book
import metrics
import suggest

CFG = suggest.load_config()
PAIR = ("Good", "binanceus")


def folds(returns):
    start = dt.date(2024, 1, 1)
    out = []
    for i, r in enumerate(returns):
        a = (pd.Timestamp(start) + pd.DateOffset(months=i)).date()
        out.append((a, (pd.Timestamp(a) + pd.DateOffset(months=1)).date(), float(r)))
    return out


def closes(monthly=0.01, days=1200):
    index = pd.date_range("2023-06-01", periods=days, freq="D")
    return pd.Series((1 + monthly) ** (np.arange(days) / 30.4), index=index) * 100


def test_a_known_return_projects_inside_its_range():
    rng = np.random.default_rng(7)
    mean, sd, inside, runs = 0.02, 0.05, 0, 200
    truth = (1 + mean) ** 12 - 1
    for i in range(runs):
        p = metrics.projection(rng.normal(mean, sd, 36), 12, i, paths=1000)
        inside += p["p5"] <= truth <= p["p95"]
        assert p["p5"] < p["p50"] < p["p95"]
    assert inside / runs > 0.90


def test_a_steady_loser_shows_a_loss_and_a_fall():
    p = metrics.projection(np.full(12, -0.03), 12, 1, paths=200)
    assert p["prob_loss"] == 1.0 and abs(p["p50"] - (0.97 ** 12 - 1)) < 1e-9
    assert abs(p["exp_max_drawdown"] - (1 - 0.97 ** 12)) < 1e-9


def test_monte_carlo_is_unchanged_by_the_shared_paths():
    r = np.random.default_rng(3).normal(0.001, 0.02, 200)
    res = r[metrics.block_indices(200, 10, 500, np.random.default_rng(9), 90)]
    assert abs(metrics.monte_carlo(r, 90, 9, paths=500)["p50"] - np.median(np.cumprod(1 + res, axis=1)[:, -1] - 1)) < 1e-12


def test_too_few_months_get_no_range():
    assert book.rows({PAIR: folds([0.01] * (CFG["confidence"]["min_folds"] - 1))}, closes(), CFG) == []


def test_a_pair_gets_both_horizons_for_itself_and_hold():
    out = book.rows({PAIR: folds([0.02, -0.01] * 6)}, closes(), CFG)
    assert {(r["basis"], r["horizon_months"]) for r in out} == {("strategy", 12), ("strategy", 60), ("hold", 12), ("hold", 60)}
    assert all(r["basis_months"] == 12 for r in out)


def test_hold_follows_btc_over_the_same_months():
    months = book.hold_months(folds([0.0] * 6), closes(monthly=0.01))
    assert len(months) == 6 and all(0.005 < m < 0.015 for m in months)


def test_no_close_before_a_month_means_no_hold():
    assert book.hold_months(folds([0.0]), closes()[pd.Timestamp("2024-06-01"):]) == []


def test_budget_traces_to_config():
    b = book.budget(CFG)
    one = b["stages"][0]
    assert (one["capital"], one["at_risk"], one["day_loss"], one["hard_stop"]) == (500, 150, 15, 75)
    assert b["vps_year"] == 12 * CFG["book"]["vps_per_month"]


def test_run_stores_only_scored_pairs_and_replaces_the_last(conn):
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('Good', 'x', 'h'), ('Bad', 'y', 'h')")
    conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) VALUES "
                 "('Good', 'binanceus', 80, false, 3, 300, '[]'), ('Bad', 'binanceus', NULL, false, 0, 0, '[]')")
    for name in ("Good", "Bad"):
        for start, end, r in folds([0.02, -0.01] * 4):
            conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, params, "
                         "tuned_profit) VALUES (%s, 'binanceus', %s, %s, %s, 'k', true, '{\"buy\": {}}', %s)",
                         (name, start, start, end, r))
    for d, c in closes().items():
        conn.execute("INSERT INTO regimes (day, btc_close) VALUES (%s, %s)", (d.date(), float(c)))
    assert book.run(conn, CFG) == {"book": 1} and book.run(conn, CFG) == {"book": 1}
    assert conn.execute("SELECT count(*), min(strategy) FROM book").fetchone() == (4, "Good")


def series(values, start="2020-01-01"):
    index = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(np.asarray(values, dtype=float), index=index.date)


def timed(values):
    return series(values).set_axis(pd.date_range("2020-01-01", periods=len(values), freq="D"))


def cfg(**kw):
    return {"book": {**CFG["book"], "benchmark_fee": 0.0, "tax_short": 0.3, "tax_long": 0.1, **kw}}


def test_tax_is_on_the_gain_at_the_rate_for_the_holding_time():
    assert book.after_tax(2.0, 100, cfg()) == 2.0 - 0.3 and book.after_tax(2.0, 400, cfg()) == 2.0 - 0.1
    assert book.after_tax(0.5, 400, cfg()) == 0.5


def test_trend_filter_acts_a_day_late_and_sits_out_the_rest_of_a_fall():
    equity, wallet, trades = book.trend_filter(timed([100, 110, 121, 60, 30, 30]), timed([90] * 6), cfg())
    assert list(equity.round(9)) == [1.0, 1.1, 1.21, 0.6, 0.6, 0.6]
    assert wallet == pytest.approx(0.6) and trades == 2


def test_a_loss_shelters_the_next_gain():
    _, wallet, trades = book.trend_filter(timed([100, 100, 50, 50, 100, 120, 240, 240, 240]), timed([90] * 9), cfg())
    assert wallet == pytest.approx(0.5 + 0.7 - 0.3 * 0.2) and trades == 4


def test_dca_counts_every_lot():
    close = timed([100.0] * 31 + [200.0] * 29)
    curve, total, after = book.dca(close, cfg())
    assert total == pytest.approx((2.0 + 1.0) / 2 - 1.0) and curve.iloc[-1] == pytest.approx(1.5)
    assert after == pytest.approx(0.5 - 0.3 * 1.0 / 2)


def test_every_benchmark_starts_together_and_a_short_history_gets_none():
    rising = (1.001 ** np.arange(1400)) * 100
    out = book.benchmark_rows(series(rising), series(rising * 2), CFG)
    assert {r["win"] for r in out} == {"1y", "3y", "all"} and len(out) == 12
    assert len({r["start_day"] for r in out if r["win"] == "all"}) == 1
    hold = next(r for r in out if r["name"] == "Hold BTC" and r["win"] == "1y")
    assert hold["max_drawdown"] == 0 and hold["after_tax_return"] < hold["total_return"]
    assert book.benchmark_rows(series(rising[:100]), series(rising[:100]), CFG) == []
