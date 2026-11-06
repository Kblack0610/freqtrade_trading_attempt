
import numpy as np
import pandas as pd
import pytest

freqtrade_strategy = pytest.importorskip("freqtrade.strategy")

from lab.research import bust  # noqa: E402
from lab.research.BustShort import signals  # noqa: E402


def series(values):
    return pd.Series(values, index=pd.date_range("2022-01-01", periods=len(values), freq="D").date, dtype=float)


def frame(close):
    days = pd.date_range("2022-01-01", periods=len(close), freq="D")
    return pd.DataFrame({"date": days, "close": close, "volume": 1.0})


def test_falls_finds_each_peak_and_trough():
    close = series([100, 120, 80, 70, 90, 130, 90])
    day = close.index
    assert bust.falls(close) == [(day[1], day[3]), (day[5], day[6])]


def test_a_small_dip_is_not_a_fall():
    assert bust.falls(series([100, 90, 100, 85, 110])) == []


def test_no_short_while_btc_is_over_its_average():
    close = np.r_[np.linspace(100, 200, 250), np.linspace(200, 50, 60)]
    rising = frame(np.linspace(100, 300, len(close)))
    assert signals(frame(close), rising)["enter_short"].sum() == 0
    assert signals(frame(close), frame(close))["enter_short"].sum() > 0


def test_a_signal_does_not_change_with_later_candles():
    close = np.r_[np.linspace(100, 200, 250), np.linspace(200, 50, 60)]
    whole = signals(frame(close), frame(close))["enter_short"]
    cut = signals(frame(close[:280]), frame(close[:280]))["enter_short"]
    assert whole.iloc[:280].tolist() == cut.tolist()


def test_funding_is_paid_on_the_stake_for_the_days_held():
    trades = pd.DataFrame({"open_date": pd.to_datetime(["2022-01-01"]), "close_date": pd.to_datetime(["2022-01-11"]),
                           "profit_abs": [100.0], "stake_amount": [1000.0]})
    assert bust.result(trades, 0.0) == (0.1, 1, 1.0)
    assert bust.result(trades, 0.001)[0] == pytest.approx(0.07)
