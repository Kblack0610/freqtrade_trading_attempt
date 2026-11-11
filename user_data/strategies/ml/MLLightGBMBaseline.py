"""A FreqAI LightGBM baseline: predict the mean close over the next 24 hourly candles, go long when
the prediction clears 1% and FreqAI marks the candle in-distribution (do_predict == 1).

It needs the ML image (the trader image built from freqtrade's _freqai tag); lab/analyze.py layers the
FreqAI settings in freqai.json beside this file and backtests it over the last ML_DAYS. Walk-forward leaves it
out (lab/walkforward.py). The label's shift(-24) is FreqAI's
target, used only to train; predictions come from models fit on earlier windows.
"""

from datetime import timedelta

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class MLLightGBMBaseline(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 40
    minimal_roi = {"0": 0.04, "720": 0.0}
    stoploss = -0.05
    max_hold = timedelta(hours=24)
    entry_threshold = 0.01

    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int, metadata: dict, **kwargs) -> DataFrame:
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)
        dataframe["%-close_to_sma-period"] = dataframe["close"] / ta.SMA(dataframe, timeperiod=period) - 1
        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        n = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-s_close"] = dataframe["close"].shift(-n).rolling(n).mean() / dataframe["close"] - 1
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return self.freqai.start(dataframe, metadata, self)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        go = (dataframe["do_predict"] == 1) & (dataframe["&-s_close"] > self.entry_threshold)
        dataframe.loc[go, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_exit(self, pair: str, trade, current_time, current_rate, current_profit, **kwargs):
        if current_time - trade.open_date_utc >= self.max_hold:
            return "time_stop"
        return None
