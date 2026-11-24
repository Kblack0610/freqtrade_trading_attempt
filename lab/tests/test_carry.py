import numpy as np
import pandas as pd
import pytest

from lab.futures.research import carry


def days(n):
    return pd.date_range("2024-01-01", periods=n, freq="D")


def market(n=400, coins=("AAA", "BBB", "CCC", "DDD", "EEE"), seed=1):
    rng = np.random.default_rng(seed)
    idx = days(n)
    close = pd.DataFrame({c: 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n))) for c in coins}, index=idx)
    funding = pd.DataFrame({c: rng.normal(1e-4, 2e-4, n) * 3 for c in coins}, index=idx)
    return funding, close


def test_settlements_sum_by_utc_day():
    rates = pd.DataFrame({"pair": ["BTC/USDT:USDT"] * 3 + ["ETH/USDT:USDT"],
                          "ts": pd.to_datetime(["2024-01-01 00:00", "2024-01-01 16:00", "2024-01-02 00:00",
                                                "2024-01-01 08:00"], utc=True),
                          "rate": [1e-4, 2e-4, 5e-4, -1e-4]})
    f = carry.daily_funding(rates)
    assert f.loc["2024-01-01", "BTC"] == pytest.approx(3e-4)
    assert f.loc["2024-01-02", "BTC"] == pytest.approx(5e-4)
    assert f.loc["2024-01-01", "ETH"] == pytest.approx(-1e-4)
    assert np.isnan(f.loc["2024-01-02", "ETH"])


def test_a_held_harvest_earns_funding_on_capital_less_one_entry():
    f = pd.DataFrame({"A": [3e-4] * 5, "B": [3e-4] * 5}, index=days(5))
    held = f.notna()
    r = carry.harvest(f, held, held)
    entry = 2 * carry.FEE / carry.CAPITAL
    assert r.iloc[0] == pytest.approx(3e-4 / carry.CAPITAL - entry)
    assert r.iloc[1:].tolist() == pytest.approx([3e-4 / carry.CAPITAL] * 4)


def test_an_empty_slot_sits_in_cash_and_does_not_resize_the_others():
    f = pd.DataFrame({"A": [3e-4] * 3, "B": [-3e-4] * 3}, index=days(3))
    listed = f.notna()
    hold = pd.DataFrame({"A": [True] * 3, "B": [False] * 3}, index=days(3))
    w = carry.weights(hold, listed)
    assert w["A"].tolist() == [0.5] * 3 and w["B"].tolist() == [0.0] * 3


def test_lowest_third_needs_three_coins():
    x = pd.DataFrame({"A": [1.0, 1.0], "B": [2.0, np.nan], "C": [3.0, np.nan]}, index=days(2))
    low = carry.lowest_third(x)
    assert low.iloc[0].tolist() == [True, False, False]
    assert not low.iloc[1].any()


def test_spread_is_low_fifth_minus_high_fifth():
    sig = pd.DataFrame([[1.0, 2, 3, 4, 5]], columns=list("ABCDE"), index=days(1))
    fwd = pd.DataFrame([[0.05, 0, 0, 0, -0.02]], columns=list("ABCDE"), index=days(1))
    assert carry.spread(sig, fwd).iloc[0] == pytest.approx(0.07)


def test_no_rule_reads_a_later_day():
    funding, close = market()
    start, end = pd.Timestamp("2024-06-01"), pd.Timestamp("2025-01-01")
    cut = pd.Timestamp("2024-09-01")
    whole = carry.study(funding, close, start, end)
    later = funding.index > cut
    f2, c2 = funding.copy(), close.copy()
    f2.loc[later] = f2.loc[later] * -5
    c2.loc[later] = c2.loc[later] * 3
    changed = carry.study(f2, c2, start, end)
    for name, r in whole.items():
        pd.testing.assert_series_equal(r.loc[:cut], changed[name].loc[:cut], check_names=False, obj=name)


def test_every_rule_measured_is_a_counted_trial():
    sql = (carry.REPO / "lab" / "migrations" / "059_carry_trials.sql").read_text()
    funding, close = market()
    names = carry.study(funding, close, pd.Timestamp("2024-06-01"), pd.Timestamp("2025-01-01"))
    assert set(names) - set(carry.TRIALS) == set(carry.BASELINES)
    for strategy, variant in carry.TRIALS.values():
        assert f"('{strategy}', '{carry.VENUE}', '{variant}')" in sql
