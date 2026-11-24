"""Short a trend break, research only. It lives outside user_data/strategies so no job or bot finds it."""

import pandas as pd
from freqtrade.strategy import IStrategy

BTC = "BTC/USDT:USDT"


def signals(df: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
    """Short a new 20-day low under the 200-day average while BTC is under its own. Cover above the 50-day average."""
    close = df["close"]
    sma200, sma50 = close.rolling(200).mean(), close.rolling(50).mean()
    b = btc[["date", "close"]].assign(btc_down=lambda x: x["close"] < x["close"].rolling(200).mean())
    down = df[["date"]].merge(b[["date", "btc_down"]], on="date", how="left")["btc_down"].fillna(False).astype(bool)
    down.index = df.index
    out = df.copy()
    out["enter_short"] = ((close < sma200) & (close < close.rolling(20).min().shift(1)) & down
                          & (df["volume"] > 0)).astype(int)
    out["exit_short"] = (close > sma50).astype(int)
    return out


class BustShort(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = True
    startup_candle_count = 200
    stoploss = -0.15
    minimal_roi = {"0": 100}

    def informative_pairs(self):
        return [(BTC, self.timeframe)]

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        return signals(dataframe, self.dp.get_pair_dataframe(BTC, self.timeframe))

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return 1.0
