"""The trend family's Donchian ensemble both ways, research only on futures candles. It lives outside
user_data/strategies so no job or bot finds it; lab/research/bust.py runs it with measured funding."""

import tomllib
from pathlib import Path

import pandas as pd
from freqtrade.strategy import IStrategy

from lab import channels

# The same settings as TrendDonchian, read from its family file.
FAMILY = tomllib.loads((Path(__file__).resolve().parents[2] / "user_data" / "strategies" / "trend" / "family.toml").read_text())
SPEC, SIZING = FAMILY["strategies"]["Donchian"], FAMILY["sizing"]


def signals(df: pd.DataFrame) -> pd.DataFrame:
    """Long while half the lookbacks break up, short while half break down; each leaves when none still does."""
    out = df.copy()
    up, down = (channels.donchian_score(df["close"], SPEC["lookbacks"], side) for side in (1, -1))
    out["scale"] = channels.vol_scale(df["close"], SIZING["vol_target"], SIZING["vol_window"], SIZING["bars_per_year"])
    out["score_long"], out["score_short"] = up, down
    ready = out["scale"].notna() & (df["volume"] > 0)
    out["enter_long"] = ((up >= SPEC["enter_at"]) & ready).astype(int)
    out["enter_short"] = ((down >= SPEC["enter_at"]) & ready).astype(int)
    out["exit_long"] = (up <= SPEC["exit_at"]).astype(int)
    out["exit_short"] = (down <= SPEC["exit_at"]).astype(int)
    return out


class DonchianLongShort(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = True
    startup_candle_count = FAMILY["startup_candle_count"]
    stoploss = FAMILY["exit"]["stoploss"]
    minimal_roi = FAMILY["exit"]["minimal_roi"]

    def populate_indicators(self, dataframe, metadata):
        return signals(dataframe)

    def populate_entry_trend(self, dataframe, metadata):
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs):
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df.empty:
            return proposed_stake
        last = df.iloc[-1]
        score = last["score_short"] if side == "short" else last["score_long"]
        return min(max(proposed_stake * float(last["scale"]) * float(score), min_stake or 0.0), max_stake)

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return 1.0
