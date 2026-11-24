"""What a league bot saw when it entered: the signal candle's columns, kept with the trade (plan
recorder-links-vivid-stream, P4).

`EntryInputs` is a strategy mixin (`class BinHV27(EntryInputs, IStrategy)`). When a trade's first entry order
fills in dry run or live, it writes the analyzed candle the entry signal came from into Freqtrade's per-trade custom
data under `KEY`: the candle's open time, the timeframe and every column with a scalar value (OHLCV, indicators,
the enter tag). `lab/crypto/ingest.py` reads it back over `/trades/.../custom-data` and writes the trade's `decisions` row.
Backtests and hyperopt skip it, so their results are unchanged.
"""

import datetime as dt
import math
from typing import Any

KEY = "entry_inputs"
MAX_COLUMNS = 80  # a strategy with more columns keeps the first 80; custom data is one text row per trade
TIMEFRAME_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def _scalar(v: Any) -> Any:
    """A JSON-safe copy of a dataframe cell, or ... for one that is not a plain scalar."""
    if hasattr(v, "item"):  # numpy scalars
        v = v.item()
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return ...


def timeframe_delta(timeframe: str) -> dt.timedelta:
    return dt.timedelta(minutes=int(timeframe[:-1]) * TIMEFRAME_MINUTES[timeframe[-1]])


def signal_candle(df, opened: dt.datetime, timeframe: str) -> dict | None:
    """The newest candle that had closed when the trade opened (its open time at least one timeframe earlier):
    the row the entry signal was read from. None when the dataframe has no such row."""
    if df is None or df.empty or "date" not in df:
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.timezone.utc)
    rows = df[df["date"] <= opened - timeframe_delta(timeframe)]
    if rows.empty:
        return None
    row = rows.iloc[-1]
    values = {}
    for col in row.index:
        if col == "date" or len(values) >= MAX_COLUMNS:
            continue
        v = _scalar(row[col])
        if v is not ...:
            values[str(col)] = v
    return {"candle": row["date"].isoformat(), "timeframe": timeframe, "values": values}


class EntryInputs:
    """Strategy mixin: list it before IStrategy so its order_filled runs, then the strategy's own."""

    def order_filled(self, pair, trade, order, current_time, **kwargs):
        from freqtrade.enums import RunMode

        if (self.dp.runmode in (RunMode.DRY_RUN, RunMode.LIVE) and order.ft_order_side == trade.entry_side
                and trade.get_custom_data(KEY) is None):
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            got = signal_candle(df, trade.open_date_utc, self.timeframe)
            if got is not None:
                trade.set_custom_data(KEY, got)
        return super().order_filled(pair, trade, order, current_time, **kwargs)
