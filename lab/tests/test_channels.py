"""lab/channels.py: every signal is causal, and the Donchian ensemble and vol scale do what they say."""

import numpy as np
import pandas as pd
import pytest

from lab import channels


def walk(n=800, seed=3):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.03, n))))


@pytest.mark.parametrize("fn", [
    lambda c: channels.donchian_score(c, [5, 20, 80]),
    lambda c: channels.donchian_score(c, [5, 20, 80], side=-1),
    lambda c: channels.vol_scale(c, 0.4, 30, 365),
    lambda c: channels.zscore(c, 24),
])
def test_a_value_never_changes_when_later_bars_arrive(fn):
    close = walk()
    whole = fn(close)
    for cut in (250, 400, 650):
        pd.testing.assert_series_equal(fn(close.iloc[:cut]), whole.iloc[:cut])


def test_a_lookback_goes_long_on_a_new_high_and_flat_on_a_half_lookback_low():
    close = pd.Series([10, 10, 10, 10, 11, 12, 11.5, 11.8, 11, 10.5], dtype=float)
    # n = 4: long at bar 4 (11 over the 4 closes before), held through the dip at bar 6, flat at bar 8
    # (11 under the lowest of the 2 before it, 11.5)
    assert channels.donchian_state(close, 4).tolist() == [0, 0, 0, 0, 1, 1, 1, 1, 0, 0]
    assert channels.donchian_state(30 - close, 4, side=-1).tolist() == [0, 0, 0, 0, 1, 1, 1, 1, 0, 0]


def test_the_score_is_the_share_of_lookbacks_long():
    close = pd.Series(np.r_[np.full(50, 100.0), np.linspace(100, 200, 50)])
    score = channels.donchian_score(close, [5, 10, 20, 40])
    assert score.iloc[30] == 0 and score.iloc[-1] == 1 and set(score.unique()) <= {0, 0.25, 0.5, 0.75, 1}


def test_vol_scale_cuts_the_wild_coin_and_caps_the_calm_one_at_one():
    rng = np.random.default_rng(0)
    calm = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.005, 200))))
    wild = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.06, 200))))
    assert channels.vol_scale(calm, 0.4, 30, 365).iloc[-1] == 1.0
    assert channels.vol_scale(wild, 0.4, 30, 365).iloc[-1] == pytest.approx(0.4 / (0.06 * 365 ** 0.5), rel=0.3)
    assert channels.vol_scale(wild, 0.4, 30, 365).iloc[:30].isna().all()
