"""DCA as a strategy, the safety-order bot: a base order on an RSI dip, then up to `safety_orders` more, the i-th at
`deviation` x i under the first fill and `scale`^i times the base order, and one take profit on the averaged
position. Long only, spot. Averaging down lowers the take-profit price and grows the loss when the dip keeps going."""

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class DCASafetyOrders(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 30
    minimal_roi = {"0": 0.02}  # on the averaged entry: freqtrade moves open_rate with each fill
    stoploss = -0.30
    position_adjustment_enable = True
    safety_orders = 4
    deviation = 0.025
    scale = 1.5
    max_entry_position_adjustment = safety_orders

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] < 35) & (dataframe["volume"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        # The base order and every safety order together spend the proposed stake.
        return proposed_stake / sum(self.scale ** i for i in range(self.safety_orders + 1))

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate, current_entry_profit, current_exit_profit,
                              **kwargs):
        if trade.has_open_orders:
            return None
        filled = trade.select_filled_orders(trade.entry_side)
        n = len(filled)  # the next safety order is number n
        if n > self.safety_orders or current_rate > filled[0].safe_price * (1 - self.deviation * n):
            return None
        return filled[0].stake_amount * self.scale ** n, f"safety_{n}"
