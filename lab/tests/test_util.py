import datetime as dt
import math

import numpy as np
import pandas as pd

from lab.util import S3_CONNECT_TIMEOUT, S3_REQUEST_TIMEOUT, finite, s3_from_env, utc

T = pd.Timestamp("2026-11-04 12:00", tz="UTC")


def test_utc_reads_every_shape_a_job_meets():
    ms = int(T.timestamp() * 1000)
    assert utc(ms) == utc(np.int64(ms)) == utc(float(ms)) == T  # epoch ms, from ccxt or freqtrade's API
    assert utc("2026-11-04 12:00:00") == T  # freqtrade's string, UTC with no offset
    assert utc("2026-11-04T14:00:00+02:00") == T
    assert utc(dt.datetime(2026, 11, 4, 12)) == utc(T.tz_convert("America/New_York")) == T
    assert utc(dt.date(2026, 11, 4)) == T.normalize()
    assert utc(None) is None and utc("") is None
    assert str(utc(ms).tz) == "UTC"


def test_finite_drops_what_postgres_and_jsonb_cannot_hold():
    assert finite(math.nan) is None and finite(math.inf) is None and finite(np.float64(-np.inf)) is None
    assert finite(np.float32(1.5)) == 1.5 and type(finite(np.float32(1.5))) is float
    assert finite(np.int64(3)) == 3 and type(finite(np.int64(3))) is int
    assert finite({"a": [1.0, math.nan, ("x", np.inf)], "b": None}) == {"a": [1.0, None, ["x", None]], "b": None}
    assert finite(T) is T and finite("s") == "s" and finite(True) is True


def test_s3_from_env_outlasts_a_stalled_home_disk(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "http://minio.minio.svc.cluster.local:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "k")
    monkeypatch.setenv("S3_SECRET_KEY", "s")
    opts = s3_from_env().__reduce__()[1][0]  # the options pyarrow rebuilds the filesystem from
    assert opts["endpoint_override"] == "minio.minio.svc.cluster.local:9000" and opts["scheme"] == "http"
    assert (opts["connect_timeout"], opts["request_timeout"]) == (S3_CONNECT_TIMEOUT, S3_REQUEST_TIMEOUT)
    assert S3_REQUEST_TIMEOUT >= 30  # the AWS SDK default, 3 s, failed a run on one stalled upload part (ar-4215)
