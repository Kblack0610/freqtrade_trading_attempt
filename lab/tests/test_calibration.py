"""lab/calibration.py: the synthetic candles carry the planted signal and nothing else, the generated strategies read
it at the configured edge, and one universe runs end to end through the real analysis, walk-forward and confidence."""

import datetime as dt
import os

import numpy as np
import pandas as pd
import pytest

from lab import calibration, suggest

C = {**suggest.load_config()["calibration"], "pairs": ["BTC/USD"]}
START, END = dt.date(2024, 1, 1), dt.date(2025, 1, 1)


@pytest.fixture(scope="module")
def bars():
    return calibration.candles(7, "BTC/USD", START, END, C)


def test_candles_are_seeded_and_consistent(bars):
    again = calibration.candles(7, "BTC/USD", START, END, C)
    assert all(bars[tf].equals(again[tf]) for tf in bars)
    shorter = calibration.candles(7, "BTC/USD", START, END - dt.timedelta(days=100), C)["1h"]
    assert shorter.equals(bars["1h"].iloc[: len(shorter)])  # a later end leaves the earlier candles as they were
    other = calibration.candles(8, "BTC/USD", START, END, C)
    assert not np.allclose(bars["1h"]["close"], other["1h"]["close"])
    m, h, d = bars["1m"], bars["1h"], bars["1d"]
    assert len(h) == 366 * 24 and len(m) == len(h) * 60 and len(d) == 366
    assert h["date"].iloc[0] == pd.Timestamp(START, tz="UTC") and d["date"].iloc[-1] == pd.Timestamp(END, tz="UTC") - pd.Timedelta(days=1)
    hour = m.set_index("date").resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    assert np.allclose(hour.to_numpy(), h[["open", "high", "low", "close"]].to_numpy())
    # strictly: an hour that opens at its high or low puts a limit entry at the open on the fill boundary
    assert (h["high"] > h[["open", "close"]].max(axis=1)).all() and (h["low"] < h[["open", "close"]].min(axis=1)).all()
    assert np.allclose(h["close"].iloc[23::24].to_numpy(), d["close"].to_numpy())


def test_returns_have_the_configured_volatility_tails_and_signal(bars):
    h = bars["1h"]
    ret = np.log(h["close"] / h["open"]).to_numpy()
    signal = 2 * np.log(h["volume"].to_numpy() / calibration.VOLUME)
    assert np.std(ret) * np.sqrt(365 * 24) == pytest.approx(C["annual_vol"], rel=0.25)
    assert pd.Series(ret).kurt() > 1  # fat tails: a normal's excess kurtosis is 0
    assert np.corrcoef(signal[1:], signal[:-1])[0, 1] == pytest.approx(C["signal_phi"], abs=0.02)
    # the signal predicts the next hour (IC about signal_ic, about 0.01 of noise over a year); the hour's own news, the
    # signal's innovation, says nothing about that hour's return, so a strategy cannot trade on the candle it reads
    ic = np.corrcoef(signal[:-1], ret[1:])[0, 1]
    assert ic == pytest.approx(C["signal_ic"], abs=0.025) and ic > 0.01
    news = signal[1:] - C["signal_phi"] * signal[:-1]
    assert abs(np.corrcoef(news, ret[1:])[0, 1]) < 0.025


def test_strategies_read_the_signal_at_their_edge(tmp_path, bars):
    """The strategy file the pipeline backtests: its feature's correlation with the signal is edge / signal_ic, a
    null's is zero, the noise is a standard normal, and a candle's feature depends on that candle only."""
    universe = calibration.universe(1, {**C, "edges": [0.04], "per_edge": 1, "nulls": 1})
    assert [s["name"] for s in universe] == ["Cal000x1", "Cal040x1"]
    calibration.write_repo(tmp_path, universe, C)
    h = bars["1h"].copy()
    signal = 2 * np.log(h["volume"] / calibration.VOLUME)
    feature = {}
    for s in universe:
        ns: dict = {}
        exec(compile((tmp_path / "user_data" / "strategies" / f"{s['name']}.py").read_text(), s["name"], "exec"), ns)
        cls = ns[s["name"]]
        full = cls.populate_indicators(object.__new__(cls), h.copy(), {"pair": "BTC/USD"})["feature"]
        half = cls.populate_indicators(object.__new__(cls), h.iloc[: len(h) // 2].copy(), {"pair": "BTC/USD"})["feature"]
        assert np.allclose(full.iloc[: len(half)], half)
        feature[s["name"]] = full
    assert np.corrcoef(feature["Cal040x1"], signal)[0, 1] == pytest.approx(0.04 / C["signal_ic"], abs=0.02)
    null = feature["Cal000x1"]
    assert abs(np.corrcoef(null, signal)[0, 1]) < 0.03
    assert null.mean() == pytest.approx(0, abs=0.03) and null.std() == pytest.approx(1, abs=0.03)


def test_an_edge_above_the_signal_is_refused():
    with pytest.raises(ValueError):
        calibration.universe(1, {**C, "edges": [C["signal_ic"] * 2]})


def test_rates_and_intervals():
    rows = [("u1", 0.0, 12, False, {"bias": True, "costs": False}), ("u1", 0.0, 12, True, {"bias": True, "costs": True}),
            ("u2", 0.0, 12, False, {"bias": False, "costs": False}), ("u1", 0.02, 12, True, {"bias": True, "costs": True})]
    t = {(r["gate"], r["edge"], r["oos_months"]): r for r in calibration.table(rows)}
    assert t["stack", 0.0, 12]["n"] == 3 and t["stack", 0.0, 12]["passed"] == 1 and t["stack", 0.0, 12]["runs"] == 2
    assert t["bias", 0.0, 12]["rate"] == pytest.approx(2 / 3)
    assert t["costs", 0.02, 12]["rate"] == 1
    lo, hi = calibration.wilson(0, 20)
    assert lo == pytest.approx(0, abs=1e-12) and hi == pytest.approx(0.161, abs=0.001)
    assert calibration.wilson(0, 0) == (None, None)


def test_one_universe_end_to_end(conn, tmp_path, monkeypatch):
    """One null and one planted edge on one pair, two folds of 30 training days: the real analysis and walk-forward
    run in the scratch schema, the outcomes land in calibration_outcomes and the rates in calibration, and the
    production tables see only the calibration run row. About three minutes, so CI runs it in its own job
    (test-calibration in .github/workflows/image.yml), beside the suite rather than in it."""
    if not os.environ.get("CALIBRATION_E2E"):
        pytest.skip("CALIBRATION_E2E not set")
    pytest.importorskip("freqtrade")
    trials = conn.execute("SELECT count(*) FROM trials").fetchone()[0]  # the migrations seed a few
    for k, v in {"WF_TRAIN_DAYS": "30", "WF_EPOCHS": "6", "WF_EPOCHS_ROI": "6", "WF_EARLY_STOP": "0", "WF_WORKERS": "2",
                 "ANALYZE_WORKERS": "2"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(calibration, "TRAIN_DAYS", 30)
    monkeypatch.setattr(calibration, "WORK", tmp_path)
    monkeypatch.setattr(calibration, "SCHEMA", "calibration_test")
    cfg = suggest.load_config()
    cfg["calibration"] = {**C, "oos_months": [1, 2], "edges": [0.04], "per_edge": 1, "nulls": 1}
    monkeypatch.setattr(suggest, "load_config", lambda path=suggest.CONFIG: cfg)
    assert calibration.main(dt.date(2026, 11, 12)) == 0
    raw, ok = conn.execute("SELECT raw, ok FROM runs WHERE kind = 'calibration'").fetchone()
    assert ok and raw["universe"] == "0001-2026-W46" and raw["folds"]["folds_left"] == 0, raw
    assert raw["folds"]["candidates"] == 2 and raw["folds"]["folds_run"] == 4, raw
    out = conn.execute("SELECT strategy, edge, oos_months, gates FROM calibration_outcomes ORDER BY 1, 3").fetchall()
    assert [(s, e, m) for s, e, m, _ in out] == [("Cal000x1", 0, 1), ("Cal000x1", 0, 2), ("Cal040x1", 0.04, 1),
                                                 ("Cal040x1", 0.04, 2)]
    assert {"bias", "costs", "deflated Sharpe"} <= set(out[0][3])
    gates = {g for g, in conn.execute("SELECT DISTINCT gate FROM calibration")}
    assert "stack" in gates and "drift" in gates
    assert conn.execute("SELECT count(*) FROM strategies").fetchone()[0] == 0  # nothing reached production tables
    assert conn.execute("SELECT count(*) FROM trials").fetchone()[0] == trials
    # the next run starts a new universe, as this one is recorded; the same week carries the same tag
    with calibration.psycopg.connect(calibration.scratch_url(), autocommit=True) as sc:
        assert sc.execute("SELECT recorded_at IS NOT NULL FROM calibration_universe").fetchone()[0]
    conn.execute(f"DROP SCHEMA {calibration.SCHEMA} CASCADE")


def test_spa_and_stepm_find_the_one_real_edge(conn):
    """SPA rejects 'nothing beats cash' when one strategy has a real daily edge, and StepM names that strategy only."""
    pytest.importorskip("arch")
    cfg = suggest.load_config()
    end = cfg["lockbox"]["start"]
    rng = np.random.default_rng(3)
    rows = [(s, "binanceus", end - dt.timedelta(days=d), float(rng.normal(mu, 0.01)), 0.0, 1)
            for s, mu in (("Real", 0.004), ("NoiseA", 0.0), ("NoiseB", -0.001)) for d in range(1, 366)]
    rows += [("Short", "binanceus", end - dt.timedelta(days=d), 0.01, 0.0, 1) for d in range(1, 60)]  # too few days
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO strategy_daily (strategy, exchange, day, ret, pnl, trades) VALUES (%s, %s, %s, %s, %s, %s)",
                        rows)
    got = calibration.spa(conn, cfg, reps=300)["binanceus"]
    assert got["strategies"] == 3 and got["days"] == 365
    assert got["spa"]["consistent"] < 0.05
    assert got["stepm"] == ["Real"]
    conn.execute("DELETE FROM strategy_daily WHERE strategy = 'Real'")  # the negative control: noise beats nothing
    got = calibration.spa(conn, cfg, reps=300)["binanceus"]
    assert got["spa"]["consistent"] > 0.05 and got["stepm"] == []
