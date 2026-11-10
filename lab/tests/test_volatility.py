import datetime as dt

import numpy as np
import pandas as pd

from lab import analyze, suggest, volatility

CFG = suggest.load_config()
LOCKBOX = CFG["lockbox"]["start"]


def candles(daily_sd: np.ndarray, start="2022-01-01", minutes=5, seed=1) -> pd.DataFrame:
    """5m closes whose returns on day i have standard deviation daily_sd[i] over the whole day."""
    per_day = 24 * 60 // minutes
    rng = np.random.default_rng(seed)
    sd = np.repeat(daily_sd, per_day) / np.sqrt(per_day)
    date = pd.date_range(start, periods=len(sd), freq=f"{minutes}min", tz="UTC")
    return pd.DataFrame({"date": date, "close": 100 * np.exp(np.cumsum(rng.normal(0, sd)))})


def daily(rv: np.ndarray, start="2021-01-01") -> pd.Series:
    return pd.Series(rv, index=pd.date_range(start, periods=len(rv), freq="D").date)


def persistent(days: int, seed: int) -> np.ndarray:
    """Log variance that drifts slowly, as crypto's does: yesterday says much about today."""
    rng = np.random.default_rng(seed)
    x = np.zeros(days)
    for t in range(1, days):
        x[t] = 0.97 * x[t - 1] + rng.normal(0, 0.25)
    return np.exp(x - 7 + rng.normal(0, 0.3, days))


def test_realized_variance_matches_the_true_variance_and_drops_thin_days():
    c = candles(np.full(40, 0.03))
    rv = volatility.realized_variance(c, 5, 0.9)
    assert len(rv) == 40 and abs(np.median(rv) / 0.03 ** 2 - 1) < 0.1
    thin = c[~((c["date"].dt.date == dt.date(2022, 1, 5)) & (c["date"].dt.hour < 6))]
    rv2 = volatility.realized_variance(thin, 5, 0.9)
    assert dt.date(2022, 1, 5) not in set(rv2.index) and len(rv2) == 39


def test_qlike_is_zero_only_for_a_perfect_forecast():
    assert volatility.qlike(np.array([2.0]), np.array([2.0]))[0] == 0
    assert (volatility.qlike(np.array([2.0, 2.0]), np.array([1.0, 4.0])) > 0).all()


def test_the_lockbox_month_is_never_scored():
    months = volatility.months_before(dt.date(2025, 10, 17), LOCKBOX)
    assert months[0] == dt.date(2025, 11, 1) and months[-1] < LOCKBOX and LOCKBOX not in months


def test_a_month_forecast_never_changes_when_later_days_arrive():
    rv = daily(persistent(1600, 3))
    months = volatility.months_before(rv.index[0], LOCKBOX)
    before = volatility.forecasts(rv, months, CFG)
    later = rv.copy()
    later[later.index >= dt.date(2024, 6, 1)] *= 50  # the future turns violent
    after = volatility.forecasts(later, months, CFG)
    keep = before["day"] < dt.date(2024, 6, 1)
    assert keep.sum() > 300
    pd.testing.assert_frame_equal(before[keep].reset_index(drop=True), after[after["day"] < dt.date(2024, 6, 1)]
                                  .reset_index(drop=True))


def test_a_month_without_enough_history_is_not_scored():
    rv = daily(persistent(300, 4), start="2025-06-01")
    f = volatility.forecasts(rv, volatility.months_before(rv.index[0], LOCKBOX), CFG)
    assert f.empty


def test_har_wins_only_when_its_interval_clears_zero():
    _, v = volatility.score("x", {f"P{i}/USD": daily(persistent(1900, i)) for i in range(4)}, CFG)
    assert {r["baseline"] for r in v} == {"mean", "ewma"} and all(r["har_wins"] for r in v)
    assert all(r["har_qlike"] < r["baseline_qlike"] for r in v)
    # negative control: a variance every forecast gets exactly right leaves nothing for HAR to win
    _, v = volatility.score("x", {"P/USD": daily(np.full(1900, 1e-3))}, CFG)
    assert v and not any(r["har_wins"] for r in v) and all(abs(r["har_qlike"]) < 1e-12 for r in v)


def test_run_rebuilds_scores_and_verdict(conn, tmp_path):
    files = {}
    for i, pair in enumerate(("BTC/USD", "ETH/USD")):
        path = tmp_path / f"{i}.feather"
        sd = np.sqrt(persistent(1000, 20 + i)) * 40
        candles(sd, start="2023-08-01", seed=i).to_feather(path)
        files[pair] = path
    files["SOL/USD"] = tmp_path / "missing.feather"
    out = volatility.run(conn, {"binanceus": files}, CFG)
    assert out["vol_scores"] > 0
    assert out["vol_forecasts"] > 0
    assert conn.execute("SELECT max(day) FROM vol_forecasts").fetchone()[0] < LOCKBOX
    models = conn.execute("SELECT model, count(DISTINCT pair) FROM vol_scores GROUP BY 1 ORDER BY 1").fetchall()
    assert models == [("ewma", 2), ("har", 2), ("mean", 2)]
    assert conn.execute("SELECT max(fold_start) FROM vol_scores").fetchone()[0] < LOCKBOX
    assert conn.execute("SELECT count(*) FROM vol_verdict WHERE exchange = 'binanceus'").fetchone()[0] == 2
    volatility.run(conn, {}, CFG)
    assert conn.execute("SELECT count(*) FROM vol_scores").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM vol_forecasts").fetchone()[0] == 0


def test_only_fully_backfilled_venues_are_read(monkeypatch, tmp_path):
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    monkeypatch.setattr(analyze, "EXCHANGES", ["binanceus", "coinbase"])
    files = analyze.vol_files({("binanceus", "5m"): dt.datetime(2024, 1, 1), ("coinbase", "1h"): dt.datetime(2024, 1, 1)},
                              "5m")
    assert set(files) == {"binanceus"}
    assert files["binanceus"]["BTC/USD"] == tmp_path / "data" / "binanceus" / "BTC_USD-5m.feather"
