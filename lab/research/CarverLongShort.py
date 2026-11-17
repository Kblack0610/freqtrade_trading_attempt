"""Carver's slow and fast trend run both ways (AFTS 6 and 8, docs/carver-afts.md), research only on futures candles.
The long halves are the forecast family's CarverEwmac64 and CarverTrendStrength16; this adds the short below a
forecast of 0. It lives outside user_data/strategies so no job or bot finds it; lab/research/bust.py runs it with
measured funding (docs/short-bust.md). No buffer: Carver's AFTS 8 buffer needs position adjustment."""

import tomllib
from pathlib import Path

import pandas as pd
from freqtrade.strategy import IStrategy

from lab import channels, forecasts

# The same settings as the long halves, read from the forecast family file.
FAMILY = tomllib.loads((Path(__file__).resolve().parents[2] / "user_data" / "strategies" / "forecast" / "family.toml").read_text())
SIZING = FAMILY["sizing"]


def signals(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """Long while the forecast is above 0, short while it is below; each side leaves when the sign turns.
    `weight` is the stake share: 1 for a flat rule, else |forecast| over its average of 10, at most 1."""
    out = df.copy()
    f = getattr(forecasts, spec["call"])(df["close"], **spec["args"])
    out["forecast"] = f
    out["scale"] = channels.vol_scale(df["close"], SIZING["vol_target"], SIZING["vol_window"], SIZING["bars_per_year"])
    out["weight"] = 1.0 if spec.get("size") == "flat" else (f.abs() / 10.0).clip(upper=1.0)
    ready = out["scale"].notna() & f.notna() & (df["volume"] > 0)
    out["enter_long"] = ((f > 0) & ready).astype(int)
    out["enter_short"] = ((f < 0) & ready).astype(int)
    out["exit_long"] = (f <= 0).astype(int)
    out["exit_short"] = (f >= 0).astype(int)
    return out


class LongShortRule:
    """What both classes share; a mixin, so each class below is its own IStrategy for freqtrade and lab/families.toml."""

    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = True
    stoploss = FAMILY["exit"]["stoploss"]
    minimal_roi = FAMILY["exit"]["minimal_roi"]
    spec: dict = {}

    def populate_indicators(self, dataframe, metadata):
        return signals(dataframe, self.spec)

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
        return min(max(proposed_stake * float(last["scale"]) * float(last["weight"]), min_stake or 0.0), max_stake)

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return 1.0


class CarverLongShort64(LongShortRule, IStrategy):
    """AFTS 6: EWMAC(64, 256) both ways, stake by volatility alone (the CarverEwmac64 settings)."""
    spec = FAMILY["strategies"]["Ewmac64"]
    # Two slow spans, so the 256-span average has settled before the first trade.
    startup_candle_count = 512


class CarverLongShort16(LongShortRule, IStrategy):
    """AFTS 8 without its buffer: EWMAC(16, 64) both ways, stake by its strength (the CarverTrendStrength16 settings)."""
    spec = FAMILY["strategies"]["TrendStrength16"]
    startup_candle_count = 128
