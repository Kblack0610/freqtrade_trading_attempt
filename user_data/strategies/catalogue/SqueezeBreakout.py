"""Volatility breakout, the squeeze: Bollinger bands (20, 2) inside Keltner channels (20 EMA, 1.5 ATR) mark a quiet
market; buy when the squeeze has ended within the last 6 candles and the close breaks over the upper Bollinger band
in an uptrend (over the 50 EMA), and sell on a close back under the band's middle. Long only, spot."""

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class SqueezeBreakout(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 200  # lets the 50 EMA settle
    minimal_roi = {"0": 100}
    stoploss = -0.08

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        mid, atr = ta.EMA(dataframe, timeperiod=20), ta.ATR(dataframe, timeperiod=20)
        dataframe["bb_upper"], dataframe["bb_mid"] = bb["upperband"], bb["middleband"]
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        squeeze = (bb["upperband"] < mid + 1.5 * atr) & (bb["lowerband"] > mid - 1.5 * atr)
        dataframe["released"] = squeeze.shift(1).rolling(6).max().eq(1) & ~squeeze
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["released"] & (dataframe["close"] > dataframe["bb_upper"])
                      & (dataframe["close"] > dataframe["ema50"]) & (dataframe["volume"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["bb_mid"], "exit_long"] = 1
        return dataframe
