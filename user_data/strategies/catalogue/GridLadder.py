"""Grid: a ladder of equal layers under a range. Buys the first layer on a close `step` under its 72-candle mean, one
more each time the price falls `step` under the last fill, and sells one layer each time it rises `step` over the
last fill, the last layer included. Long only, spot. A trend down walks it to the stoploss: that is the grid's risk."""

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class GridLadder(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 72
    minimal_roi = {"0": 100}
    stoploss = -0.25
    position_adjustment_enable = True
    layers = 5
    step = 0.02
    max_entry_position_adjustment = -1  # held layers are capped in adjust_trade_position, not total buys

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["center"] = dataframe["close"].rolling(self.startup_candle_count).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] <= dataframe["center"] * (1 - self.step)) & (dataframe["volume"] > 0),
                      "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        return proposed_stake / self.layers

    @staticmethod
    def last_fill(trade) -> float:
        return trade.select_filled_orders()[-1].safe_price

    def held(self, trade) -> int:
        return trade.nr_of_successful_entries - trade.nr_of_successful_exits

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                              current_entry_rate, current_exit_rate, current_entry_profit, current_exit_profit,
                              **kwargs):
        if trade.has_open_orders:
            return None
        held, last = self.held(trade), self.last_fill(trade)
        if current_rate <= last * (1 - self.step) and held < self.layers:
            return trade.select_filled_orders(trade.entry_side)[0].stake_amount, "grid_down"
        if current_rate >= last * (1 + self.step) and held > 1:
            return -trade.stake_amount / held, "grid_up"
        return None

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        if self.held(trade) <= 1 and current_rate >= self.last_fill(trade) * (1 + self.step):
            return "grid_top"
        return None
