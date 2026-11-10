"""Cross-sectional momentum, a rotation: each day rank the whitelist by 14-day return, hold the top `top` coins whose
own return is positive (dual momentum), and sell one when it drops out of the top `top + 1` or its return turns
negative. Daily candles, long or flat, spot: the short side of the ranking is the part a spot account cannot hold."""

import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class XSMomentumRotation(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = False
    lookback = 14
    top = 3
    startup_candle_count = 30
    minimal_roi = {"0": 100}
    stoploss = -0.30

    def universe(self) -> list[str]:
        # The whitelist as configured (freqtrade keeps it in original_config), not the live one: a lookahead check's
        # partial runs whitelist only the pair under test, which would rank it against nothing. The league runs a
        # static pair list, so the two are the same in a dry run.
        return self.config.get("original_config", self.config)["exchange"]["pair_whitelist"]

    def informative_pairs(self):
        return [(pair, self.timeframe) for pair in self.universe()]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        returns = {}
        for pair in self.universe():
            other = self.dp.get_pair_dataframe(pair, self.timeframe)
            if not other.empty:
                returns[pair] = other.set_index("date")["close"].pct_change(self.lookback)
        rank = pd.DataFrame(returns).rank(axis=1, ascending=False)
        mine = rank.get(metadata["pair"], pd.Series(dtype=float, index=rank.index))
        dataframe = dataframe.merge(mine.rename("rank").rename_axis("date").reset_index(), on="date", how="left")
        dataframe["ret"] = dataframe["close"].pct_change(self.lookback)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rank"] <= self.top) & (dataframe["ret"] > 0) & (dataframe["volume"] > 0),
                      "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rank"] > self.top + 1) | (dataframe["ret"] < 0), "exit_long"] = 1
        return dataframe
