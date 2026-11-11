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


def candles(n=1200, seed=5, freq="h"):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    opn = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.01, n))
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC"), "open": opn,
                         "high": np.maximum(opn, close) * (1 + wick), "low": np.minimum(opn, close) * (1 - wick),
                         "close": close, "volume": np.exp(rng.normal(3, 0.8, n))})


@pytest.mark.parametrize("fn", [
    lambda d: channels.atr_break(d, 14, 1.0),
    lambda d: channels.keltner_break(d, 20, 2.0),
    lambda d: channels.squeeze_release(d, 20, 2.0, 1.5, 6),
    lambda d: channels.clock(d, 20),
    lambda d: channels.volume_surge(d, 48, 3.0),
    lambda d: channels.obv_break(d, 50),
], ids=["atr", "keltner", "squeeze", "clock", "surge", "obv"])
def test_a_frame_signal_never_changes_when_later_bars_arrive_and_fires(fn):
    df = candles()
    whole = fn(df)
    assert whole.dtype == bool and whole.sum() > 0
    for cut in (300, 700, 1000):
        pd.testing.assert_series_equal(fn(df.iloc[:cut]), whole.iloc[:cut])


def test_obv_break_does_not_depend_on_where_the_data_starts():
    df = candles()
    pd.testing.assert_series_equal(channels.obv_break(df.iloc[400:], 50).iloc[100:],
                                   channels.obv_break(df, 50).iloc[500:])


@pytest.mark.parametrize("freq", ["15min", "h", "4h", "D"])
def test_the_clock_fires_on_the_candle_that_closes_at_the_hour(freq):
    df = candles(freq=freq, n=2000)
    close = df["date"] + pd.tseries.frequencies.to_offset(freq)
    fired = close[channels.clock(df, 0, weekdays=[5])]
    assert len(fired) > 0 and (fired.dt.hour == 0).all() and (fired.dt.minute == 0).all() and (fired.dt.weekday == 5).all()
