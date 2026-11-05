"""A deliberately broken strategy: it buys on the next candle's close. lab/analyze.py must flag it."""

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class PlantedLookahead(IStrategy):
    timeframe = "5m"
    minimal_roi = {"0": 0.01}
    stoploss = -0.05
    startup_candle_count = 20

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["next_close"] = dataframe["close"].shift(-1)
        dataframe["sma"] = dataframe["close"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["next_close"] > dataframe["close"] * 1.002, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma"], "exit_long"] = 1
        return dataframe
