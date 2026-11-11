"""Helpers every lab job shares: one way to read a timestamp, one way to drop a number Postgres or JSON cannot hold."""
import math
import numbers
import os
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from pyarrow import fs as pafs


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


def s3_from_env() -> pafs.FileSystem:
    """The object store (MinIO) from S3_ENDPOINT, S3_ACCESS_KEY and S3_SECRET_KEY."""
    url = urlparse(os.environ["S3_ENDPOINT"])
    return pafs.S3FileSystem(access_key=os.environ["S3_ACCESS_KEY"], secret_key=os.environ["S3_SECRET_KEY"],
                             endpoint_override=url.netloc, scheme=url.scheme or "https", region="us-east-1")
