import datetime as dt

import numpy as np
import pandas as pd
import pytest

from lab.crypto import book, suggest
from lab import metrics

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


def test_hard_stop_share_counts_the_planted_paths():
    # Two months, one block of 2: each path is the whole series or its mirror start; -20% then +10% falls 20%, +10% then -20% too.
    # Months [-0.2, 0.0] with block 1 draw a -20% month in a path of 2 with probability 3/4, so the share is near 0.75.
    ret = np.array([-0.2, 0.0])
    assert metrics.hard_stop_share(np.full(12, -0.03), 12, 1, 0.30, paths=200) == 1.0
    assert metrics.hard_stop_share(np.full(12, -0.03), 12, 1, 0.40, paths=200) == 0.0
    share = metrics.hard_stop_share(ret, 2, 5, 0.15, paths=20000, block=1)
    assert abs(share - 0.75) < 0.02
    assert metrics.hard_stop_share(ret, 2, 5, 0.15, paths=2000, block=1) == metrics.hard_stop_share(ret, 2, 5, 0.15, paths=2000, block=1)


def test_stop_rows_give_each_stage_the_share_of_the_projection_paths():
    f = {PAIR: folds([-0.2, 0.0] * 3)}
    cfg = {**CFG, "book": {**CFG["book"], "block_months": 1,
                           "stages": [{"name": "A", "capital": 1.0}, {"name": "B", "capital": 1.0}]}}
    a, b = book.stop_rows(f, cfg)
    assert (a["stage"], a["hard_stop"], b["stage"], b["hard_stop"]) == ("A", CFG["book"]["hard_stop"], "B", CFG["book"]["hard_stop"])
    assert a["p_hard_stop"] > 0.99 and b["p_hard_stop"] == a["p_hard_stop"] and a["horizon_months"] == 12
    assert a["p_hard_stop"] == metrics.hard_stop_share([x[2] for x in f[PAIR]], 12, metrics.seed_of(*PAIR, "strategy", 12), a["hard_stop"], block=1)


def test_monte_carlo_is_unchanged_by_the_shared_paths():
    r = np.random.default_rng(3).normal(0.001, 0.02, 200)
    res = metrics.block_sample(r, 10, 500, np.random.default_rng(9), 90)
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


def test_a_record_shorter_than_z_over_sharpe_squared_gets_a_zero_ceiling():
    sigma, sharpe = 0.6, 1.0
    short = (CFG["book"]["kelly_z"] / sharpe) ** 2 * 0.9
    one = book.budget(CFG, mu=sharpe * sigma, sigma=sigma, years=short)["stages"][0]
    assert one["kelly_ceiling"] == 0 and one["over_kelly"]
    assert book.budget(CFG, mu=sharpe * sigma, sigma=sigma, years=0)["stages"][0]["kelly_ceiling"] == 0


def test_a_planted_basket_gives_the_hand_computed_ceiling():
    # Daily returns: A +2% -2%, B flat, C +1% -1%, so the basket goes +1% -1% over 4 days.
    closes = pd.DataFrame({"A": [100, 102, 99.96, 101.9592, 99.920016], "B": [100.0] * 5,
                           "C": [100, 101, 99.99, 100.9899, 99.980001]})
    sigma = book.basket_sigma(closes)
    assert sigma == pytest.approx(0.01 * (4 / 3) ** 0.5 * 365 ** 0.5)  # 0.220605
    # Lower bound 0.3 - 1.645 * 0.220605 / 3 = 0.179035; a quarter of that over 0.220605^2 is 0.919699 of capital.
    stages = book.budget(CFG, mu=0.3, sigma=sigma, years=9)["stages"]
    one = stages[0]
    assert one["kelly_ceiling"] == pytest.approx(459.8497, abs=1e-3) and one["over_kelly"] is False
    assert [s["kelly_ceiling"] / s["capital"] for s in stages] == pytest.approx([0.919699] * len(stages), abs=1e-6)


def test_the_ceiling_never_passes_capital_and_unknown_inputs_mark_nothing():
    assert book.kelly_ceiling(500, mu=5.0, sigma=0.2, years=10, cfg=CFG) == 500
    for inputs in ({"mu": 0.3}, {"mu": 0.3, "sigma": 0.0, "years": 2}, {"mu": float("nan"), "sigma": 0.2, "years": 2}):
        assert all(s["kelly_ceiling"] is None and s["over_kelly"] is None for s in book.budget(CFG, **inputs)["stages"])


def test_a_coin_listed_later_still_counts_from_its_first_day():
    closes = pd.DataFrame({"A": [100, 101, 100, 101.0], "B": [None, None, 100, 102.0]})
    assert book.basket_sigma(closes) == pytest.approx(pd.Series([0.01, 100 / 101 - 1, (0.01 + 0.02) / 2]).std() * 365 ** 0.5)


def test_day_rows_count_zero_padded_days_scaled_to_the_stage_risk():
    cfg = {**CFG, "watcher": {**CFG["watcher"], "day_loss": 0.05},
           "book": {**CFG["book"], "stages": [{"name": "A", "capital": 100.0, "stake": 10.0, "max_open": 2},
                                              {"name": "B", "capital": 400.0, "stake": 10.0, "max_open": 2}]}}
    ret = pd.Series({dt.date(2024, 1, 2): -0.5, dt.date(2024, 1, 3): -0.2, dt.date(2024, 1, 4): 0.3})
    a, b = book.day_rows({PAIR: (dt.date(2024, 1, 1), dt.date(2024, 1, 11))}, {PAIR: ret}, cfg)
    # A scales by 0.2: -0.1 and -0.04, so one day reaches -5%. B scales by 0.05: neither does.
    assert (a["days"], a["limit_days"], a["p_day_limit"], a["day_limit"]) == (10, 1, 0.1, 0.05)
    assert (b["stage"], b["limit_days"], b["p_day_limit"]) == ("B", 0, 0.0)
    assert book.day_rows({PAIR: (dt.date(2024, 1, 1), dt.date(2024, 1, 3))}, {}, cfg)[0]["p_day_limit"] == 0.0


def test_run_stores_only_scored_pairs_and_replaces_the_last(conn):
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('Good', 'x', 'h'), ('Bad', 'y', 'h')")
    conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) VALUES "
                 "('crypto', 'Good', 'binanceus', 80, false, 3, 300, '[]'), ('crypto', 'Bad', 'binanceus', NULL, false, 0, 0, '[]')")
    for name in ("Good", "Bad"):
        for start, end, r in folds([0.02, -0.01] * 4):
            conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, params, "
                         "tuned_profit) VALUES (%s, 'binanceus', %s, %s, %s, 'k', true, '{\"buy\": {}}', %s)",
                         (name, start, start, end, r))
    for d, c in closes().items():
        conn.execute("INSERT INTO regimes (day, btc_close) VALUES (%s, %s)", (d.date(), float(c)))
    assert book.run(conn, CFG) == {"book": 1} == book.run(conn, CFG)
    assert conn.execute("SELECT count(*), min(strategy) FROM book").fetchone() == (4, "Good")
    assert conn.execute("SELECT count(*), min(strategy) FROM book_stops").fetchone() == (len(CFG["book"]["stages"]), "Good")
    assert conn.execute("SELECT count(*), min(days) > 0 FROM book_days").fetchone() == (len(CFG["book"]["stages"]), True)
    book.run(conn, {**CFG, "lockbox": {"start": dt.date(2000, 1, 1)}})
    assert conn.execute("SELECT count(*) FROM book").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM book_stops").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM book_days").fetchone() == (0,)


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


def test_run_books_the_months_before_the_lockbox(conn):
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('A', 'x', 'h'), ('B', 'y', 'h')")
    for name in ("A", "B"):
        conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) VALUES "
                     "('crypto', %s, 'x', 80, false, 3, 300, '[]')", (name,))
        for start, end, r in folds([0.02, -0.01] * 4):
            conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, params, "
                         "tuned_profit) VALUES (%s, 'x', %s, %s, %s, 'k', true, '{\"buy\": {}}', %s)", (name, start, start, end, r))
    for d, c in closes().items():
        conn.execute("INSERT INTO regimes (day, btc_close) VALUES (%s, %s)", (d.date(), float(c)))
    assert book.run(conn, CFG) == {"book": 2}
    assert conn.execute("SELECT max(basis_months) FROM book").fetchone()[0] == 8


def test_a_strategy_pays_tax_every_year_and_hold_once():
    band = {"horizon_months": 60, "p50": 1.0}
    strategy = book.median_after_tax({**band, "basis": "strategy"}, CFG)
    hold = book.median_after_tax({**band, "basis": "hold"}, CFG)
    assert hold == pytest.approx(1.0 * (1 - CFG["book"]["tax_long"]))
    assert strategy < hold


def test_a_loss_pays_no_tax():
    band = {"horizon_months": 12, "p50": -0.2, "basis": "strategy"}
    assert book.median_after_tax(band, CFG) == pytest.approx(-0.2)


def test_the_hurdle_compounds_and_tax_lowers_it():
    one, five = book.hurdle(CFG)
    assert one["total"] == pytest.approx(CFG["book"]["tbill_yield"])
    assert five["total"] > 5 * one["total"]
    assert one["after_tax"] == pytest.approx(one["total"] * (1 - CFG["book"]["tax_short"]))
