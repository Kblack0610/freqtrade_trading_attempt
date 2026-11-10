"""Relative value, the long leg of pairs trading: buy a coin when its price against BTC sits `z_enter` deviations
under its `window`-candle mean, and sell when the ratio is back at the mean. Spot has no short, so the BTC leg is
not hedged and the trade carries the market's move as well as the ratio's; the market-neutral version needs a short
and is blocked by lab/league.py (docs/strategy-coverage.md). BTC itself never trades."""

import numpy as np
from pandas import DataFrame

from freqtrade.strategy import IStrategy
from lab import channels


class RatioRevert(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    window = 168
    z_enter = -2.0
    z_exit = 0.0
    startup_candle_count = 200
    minimal_roi = {"0": 100}
    stoploss = -0.10

    def btc(self) -> str:
        return f"BTC/{self.config['stake_currency']}"

    def informative_pairs(self):
        return [(self.btc(), self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if metadata["pair"] == self.btc():
            dataframe["z"] = np.nan
            return dataframe
        btc = self.dp.get_pair_dataframe(self.btc(), self.timeframe)[["date", "close"]]
        dataframe = dataframe.merge(btc.rename(columns={"close": "btc"}), on="date", how="left")
        dataframe["z"] = channels.zscore(np.log(dataframe["close"]) - np.log(dataframe["btc"]), self.window)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["z"] <= self.z_enter) & (dataframe["volume"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["z"] >= self.z_exit, "exit_long"] = 1
        return dataframe
