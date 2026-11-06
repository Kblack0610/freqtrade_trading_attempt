"""Helpers every lab job shares: one way to read a timestamp, one way to drop a number Postgres or JSON cannot hold."""
import math
import numbers

import numpy as np
import pandas as pd


def utc(value) -> pd.Timestamp | None:
    """Any timestamp as a UTC pd.Timestamp: epoch milliseconds (as exchanges and freqtrade's API send them),
    an ISO string (freqtrade's 'YYYY-MM-DD HH:MM:SS' has no offset and is UTC), a date or a datetime.
    None or an empty string is None."""
    if value is None or (isinstance(value, str) and not value):
        return None
    t = pd.Timestamp(value, unit="ms") if isinstance(value, numbers.Real) else pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def finite(v):
    """A value Postgres and jsonb can store: NaN and +-Infinity become None (jsonb has neither, and Python's
    json writes both), numpy scalars become Python ones, and dicts, lists and tuples are cleaned throughout."""
    if isinstance(v, (float, np.floating)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, dict):
        return {k: finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [finite(x) for x in v]
    return v
