"""The FreqAI family in user_data/strategies/ml/. The train-and-predict tests need lightgbm, so they run
only in the ML image (the trader image built on freqtrade 2026.9_freqai, FREQTRADE_IMAGE); the plain image
skips them and checks the strategy is recorded as needing that image."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from test_families import synthetic_candles

LAB = Path(__file__).resolve().parents[1]
REPO = LAB.parent
ML = REPO / "user_data" / "strategies" / "ml"


def test_the_nightly_analysis_scans_the_ml_family_and_needs_the_ml_image_for_it():
    from lab import analyze

    ml = {s["name"]: s for s in analyze.Cli().discover()}["MLLightGBMBaseline"]
    assert ml["timeframe"] == "1h" and analyze.freqai_fragment(ml["file"])["freqai"]["enabled"]
    assert ml["load_error"] == (None if analyze.FREQAI else analyze.NEEDS_ML)


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


def test_a_real_chunk_trains_under_its_own_identifier_and_reuses_its_models(tmp_path, monkeypatch):
    pytest.importorskip("lightgbm")
    import shutil

    from lab import analyze

    ud = tmp_path / "ud"
    shutil.copytree(REPO / "user_data" / "exchanges", ud / "exchanges")
    shutil.copy2(REPO / "user_data" / "config.base.json", ud)
    shutil.copytree(ML, ud / "strategies" / "ml")
    data = tmp_path / "lab" / "data" / "binanceus"
    data.mkdir(parents=True)
    utc = dt.timezone.utc
    for i, pair in enumerate(analyze.pair_list("binanceus")):
        synthetic_candles(dt.datetime(2026, 1, 1, tzinfo=utc), dt.datetime(2026, 2, 20, tzinfo=utc), "h", seed=i).to_feather(
            data / f"{pair.replace('/', '_')}-1h.feather")
    monkeypatch.setattr(analyze, "USER_DATA", ud)
    monkeypatch.setattr(analyze, "RESULTS", tmp_path / "lab")
    s = {"name": "MLLightGBMBaseline", "file": "user_data/strategies/ml/MLLightGBMBaseline.py", "file_hash": "h"}
    start, end = dt.date(2026, 2, 1), dt.date(2026, 2, 15)
    cfg = analyze.chunk_config(s, "binanceus", start)
    res = analyze.Cli().backtest(s["name"], "binanceus", start, end, cfg)
    assert res["ok"], res["error"]
    models = ud / "models" / cfg["freqai"]["identifier"]
    assert len(list((models / "backtesting_predictions").glob("*_prediction.feather"))) == 2 * len(analyze.pair_list("binanceus"))
    again = analyze.Cli().backtest(s["name"], "binanceus", start, end, cfg)
    print(f"first {res['duration_s']:.0f}s, again {again['duration_s']:.0f}s")
    assert again["ok"] and again["duration_s"] < res["duration_s"]
    assert len(again["trades"]) == len(res["trades"])
