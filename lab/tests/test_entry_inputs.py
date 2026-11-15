import datetime as dt

import numpy as np
import pandas as pd
from freqtrade.enums import RunMode

from lab.entry_inputs import KEY, EntryInputs, signal_candle

OPENED = dt.datetime(2026, 11, 4, 12, 17, 3, tzinfo=dt.timezone.utc)


def frame() -> pd.DataFrame:
    dates = pd.date_range("2026-11-04 12:00", periods=4, freq="5min", tz="UTC")  # 12:00 .. 12:15
    return pd.DataFrame({"date": dates, "close": [1.0, 2.0, 3.0, 4.0], "rsi": [np.float64(30), np.nan, 31.5, 40.0],
                         "enter_long": np.array([0, 0, 1, 0], dtype=np.int64), "enter_tag": [None, None, "dip", None],
                         "bands": [[1], [2], [3], [4]]})


def test_the_signal_candle_is_the_newest_one_closed_when_the_trade_opened():
    got = signal_candle(frame(), OPENED, "5m")  # 12:15 was still open at 12:17; 12:10 had closed
    assert got == {"candle": "2026-11-04T12:10:00+00:00", "timeframe": "5m",
                   "values": {"close": 3.0, "rsi": 31.5, "enter_long": 1, "enter_tag": "dip"}}  # a list cell is dropped
    assert signal_candle(frame(), OPENED - dt.timedelta(minutes=5), "5m")["values"]["rsi"] is None  # NaN -> null


def test_no_candle_old_enough_is_none():
    assert signal_candle(frame(), dt.datetime(2026, 11, 4, 12, 3, tzinfo=dt.timezone.utc), "5m") is None
    assert signal_candle(pd.DataFrame(), OPENED, "1h") is None


class Base:
    def order_filled(self, pair, trade, order, current_time, **kwargs):
        self.base_called = True


class Strategy(EntryInputs, Base):
    timeframe = "5m"

    def __init__(self, runmode):
        self.dp = type("DP", (), {"runmode": runmode, "get_analyzed_dataframe": lambda s, p, tf: (frame(), None)})()


class FakeTrade:
    entry_side = "buy"
    open_date_utc = OPENED

    def __init__(self):
        self.data = {}

    def get_custom_data(self, key):
        return self.data.get(key)

    def set_custom_data(self, key, value):
        self.data[key] = value


def fill(strategy, trade, side="buy"):
    strategy.order_filled("BTC/USD", trade, type("Order", (), {"ft_order_side": side})(), OPENED)


def test_the_first_entry_fill_records_the_candle_once_and_the_strategy_still_runs():
    s, t = Strategy(RunMode.DRY_RUN), FakeTrade()
    fill(s, t)
    first = t.data[KEY]
    assert first["candle"] == "2026-11-04T12:10:00+00:00" and s.base_called
    t.open_date_utc = OPENED + dt.timedelta(minutes=10)  # a later entry (position adjustment) keeps the first
    fill(s, t)
    fill(s, t, side="sell")
    assert t.data[KEY] is first


def test_backtests_record_nothing():
    for mode in (RunMode.BACKTEST, RunMode.HYPEROPT):
        s, t = Strategy(mode), FakeTrade()
        fill(s, t)
        assert t.data == {} and s.base_called
