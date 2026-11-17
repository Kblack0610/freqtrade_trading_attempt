"""lab/forecasts.py: Carver's forecasts are causal, capped, signed the way the book says, and the generated family runs them."""

import numpy as np
import pandas as pd
import pytest

from lab import forecasts


def walk(n=1500, seed=3, drift=0.0):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, 0.03, n))))


CASES = {
    "hold": lambda c: forecasts.hold_forecast(c),
    "ewmac": lambda c: forecasts.ewmac(c, 16),
    "mix": lambda c: forecasts.ewmac_mix(c, [8, 16, 32, 64], 1.13),
    "regime": lambda c: forecasts.regime_mix(c, [8, 16, 32, 64], 1.13, 300),
    "range": lambda c: forecasts.breakout_forecast(c, 80),
    "accel": lambda c: forecasts.acceleration(c, 16, 6.0),
    "skew": lambda c: forecasts.skew_forecast(c, 90, 10.0),
    "dip": lambda c: forecasts.dip_forecast(c, 5, 1, 2.0, 24),
    "dip_safe": lambda c: forecasts.dip_forecast(c, 5, 1, 2.0, 24, trend_fast=16),
}


@pytest.mark.parametrize("fn", CASES.values(), ids=CASES.keys())
def test_a_forecast_never_changes_when_later_bars_arrive(fn):
    close = walk()
    whole = fn(close)
    for cut in (400, 900, 1300):
        pd.testing.assert_series_equal(fn(close.iloc[:cut]), whole.iloc[:cut])


@pytest.mark.parametrize("fn", CASES.values(), ids=CASES.keys())
def test_a_forecast_that_reads_the_next_bar_fails_the_causality_check(fn):
    """The negative control: the same comparison, on a forecast that peeks one bar ahead, must differ."""
    close = walk()
    peek = lambda c: fn(c).shift(-1)  # noqa: E731
    if fn(close).nunique() <= 1:
        pytest.skip("a constant forecast has nothing to peek at")
    with pytest.raises(AssertionError):
        pd.testing.assert_series_equal(peek(close.iloc[:900]), peek(close).iloc[:900])


@pytest.mark.parametrize("fn", CASES.values(), ids=CASES.keys())
def test_a_forecast_stays_inside_the_cap(fn):
    out = fn(walk(seed=5)).dropna()
    assert len(out) > 0 and out.abs().max() <= forecasts.CAP + 1e-9


def test_ewmac_is_positive_in_an_uptrend_and_negative_in_a_downtrend():
    up, down = walk(drift=0.01), walk(drift=-0.01)
    assert forecasts.ewmac(up, 16).iloc[-200:].mean() > 0
    assert forecasts.ewmac(down, 16).iloc[-200:].mean() < 0


def test_the_mean_absolute_ewmac_forecast_is_near_the_books_ten():
    out = pd.concat([forecasts.ewmac(walk(5000, seed=s), 16) for s in range(4)]).dropna()
    assert 6 < out.abs().mean() < 14


def test_the_regime_multiplier_runs_from_two_in_calm_to_one_half_in_wild():
    rng = np.random.default_rng(1)
    vol = np.r_[np.full(400, 0.02), np.linspace(0.02, 0.2, 200), np.full(100, 0.2)]
    calm_after_wild = np.r_[vol, np.full(400, 0.002)]
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 1, len(calm_after_wild)) * calm_after_wild)))
    m = forecasts.vol_multiplier(close, 300).dropna()
    assert m.between(0.5, 2.0).all()
    assert m.iloc[-1] > 1.7
    assert m.loc[650:700].max() < 1.0  # inside the wild stretch


def test_the_breakout_forecast_is_high_at_the_top_of_its_range_and_low_at_the_bottom():
    up = pd.Series(np.linspace(100, 200, 200))
    assert forecasts.breakout_forecast(up, 40).iloc[-1] > 10
    assert forecasts.breakout_forecast(up[::-1].reset_index(drop=True), 40).iloc[-1] < -10


def test_the_skew_forecast_is_long_after_a_crash_and_not_after_a_melt_up():
    rng = np.random.default_rng(2)
    r = rng.normal(0, 0.01, 200)
    crashy, meltup = r.copy(), r.copy()
    crashy[-5:] = -0.15
    meltup[-5:] = 0.15
    assert forecasts.skew_forecast(pd.Series(100 * np.exp(crashy.cumsum())), 90, 10.0).iloc[-1] > 0
    assert forecasts.skew_forecast(pd.Series(100 * np.exp(meltup.cumsum())), 90, 10.0).iloc[-1] < 0


def test_the_dip_is_long_under_its_equilibrium_and_the_safe_one_stands_aside_in_a_downtrend():
    fall = pd.Series(100 * np.exp(-0.002 * np.arange(600)) * (1 + 0.003 * np.sin(np.arange(600))))
    plain = forecasts.dip_forecast(fall, 5, 1, 2.0, 24)
    safe = forecasts.dip_forecast(fall, 5, 1, 2.0, 24, trend_fast=16)
    assert (plain.iloc[-100:] > 0).any()
    assert (safe.iloc[-100:] == 0).all()
