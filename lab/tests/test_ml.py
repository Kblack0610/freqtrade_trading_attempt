"""The staged FreqAI family in lab/families/ml/. The train-and-predict test needs lightgbm, so it runs
only in the ML image (the trader image built on freqtrade 2026.9_freqai, FREQTRADE_IMAGE); the plain image
skips it. The not-scanned check runs everywhere."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from test_families import synthetic_candles

LAB = Path(__file__).resolve().parents[1]
ML = LAB / "families" / "ml"
REPO = LAB.parent


def test_the_nightly_analysis_does_not_scan_the_ml_family():
    from lab import analyze

    assert "MLLightGBMBaseline" not in {s["name"] for s in analyze.Cli().discover()}


def test_lightgbm_trains_on_earlier_windows_and_predicts_only_after_them(tmp_path):
    pytest.importorskip("lightgbm")
    data = tmp_path / "data"
    data.mkdir()
    utc = dt.timezone.utc
    synthetic_candles(dt.datetime(2026, 1, 1, tzinfo=utc), dt.datetime(2026, 2, 20, tzinfo=utc), "h").to_feather(
        data / "BTC_USD-1h.feather")
    ud = tmp_path / "ud"
    for sub in ("models", "backtest_results", "logs", "data"):
        (ud / sub).mkdir(parents=True)
    cmd = ["freqtrade", "backtesting", "--userdir", str(ud), "--datadir", str(data),
           "-c", str(REPO / "user_data" / "config.base.json"), "-c", str(REPO / "user_data" / "exchanges" / "binanceus.json"),
           "-c", str(ML / "freqai.json"), "--strategy-path", str(ML), "-s", "MLLightGBMBaseline",
           "--timerange", "20260201-20260215", "-p", "BTC/USD", "--cache", "none"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, (p.stdout + p.stderr)[-3000:]

    ident = json.loads((ML / "freqai.json").read_text())["freqai"]["identifier"]
    files = sorted((ud / "models" / ident / "backtesting_predictions").glob("*_prediction.feather"))
    assert len(files) == 2  # two 7-day windows, each from a model fit on the 30 days before it
    for f in files:
        trained_until = pd.Timestamp(int(f.name.split("_")[-2]), unit="s", tz="UTC")
        pred = pd.read_feather(f)
        assert pred["date"].min() >= trained_until and len(pred) == 7 * 24, f.name
        assert pred["&-s_close"].std() > 0 and pred["do_predict"].isin([0, 1]).all(), f.name
