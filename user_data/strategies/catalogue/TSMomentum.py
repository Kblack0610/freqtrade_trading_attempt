"""Time-series momentum: hold a coin while both its 30-day and 90-day returns are positive, and leave when the 30-day
return turns negative. Daily candles, long or flat, spot."""

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class TSMomentum(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = False
    fast, slow = 30, 90
    startup_candle_count = 100
    minimal_roi = {"0": 100}
    stoploss = -0.30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["r_fast"] = dataframe["close"].pct_change(self.fast)
        dataframe["r_slow"] = dataframe["close"].pct_change(self.slow)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["r_fast"] > 0) & (dataframe["r_slow"] > 0) & (dataframe["volume"] > 0),
                      "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["r_fast"] < 0, "exit_long"] = 1
        return dataframe
