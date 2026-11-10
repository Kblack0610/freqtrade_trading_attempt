"""Generated strategy families: files current, every strategy loadable by
name, and no lookahead on synthetic prices through the same check the nightly analysis runs."""

import datetime as dt
import pkgutil
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.families import generate

LAB = Path(__file__).resolve().parents[1]
FAMILIES = LAB / "families"
ACTIVE = LAB.parent / "user_data" / "strategies"
CANDLES = ACTIVE / "candles"
CHART = ACTIVE / "chart"
TREND = FAMILIES / "trend"
RANGE = FAMILIES / "range"
PLANTED = LAB / "tests" / "fixtures" / "strategies"
CATALOGUE = LAB.parent / "user_data" / "strategies" / "catalogue"  # scanned, so these are counted trials


def test_every_family_file_is_what_the_generator_writes():
    assert {f.name for f in generate.families()} >= {"candles", "chart", "trend", "range"}
    for fam in generate.families():
        assert generate.stale(fam) == [], f"rerun python lab/families/generate.py ({fam.name})"


def test_the_skipped_candle_patterns_are_bearish_only():
    import talib

    skip = tomllib.loads((CANDLES / "family.toml").read_text())["skip"]["bearish_only"]
    # Random candles with gapped opens, a share of dojis and random wicks: each skipped pattern
    # shows up bearish in it at least once, and never bullish.
    rng = np.random.default_rng(0)
    n = 400_000
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, 0.003, n))
    o = np.where(rng.random(n) < 0.1, c * np.exp(rng.normal(0, 0.0005, n)), o)
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    for p in skip:
        v = getattr(talib, p)(o, h, lo, c)
        assert (v > 0).sum() == 0 and (v < 0).sum() > 0, p


def test_every_family_strategy_loads_by_name():
    """freqtrade finds a strategy by name only in a file containing `class <Name>(`; this is the load
    a backtest does, which list-strategies alone does not prove."""
    from lab import analyze
    from freqtrade.configuration import Configuration
    from freqtrade.resolvers import StrategyResolver

    base = Configuration.from_files([str(p) for p in analyze.config_files(analyze.BIAS_EXCHANGE)])
    for fam in generate.families():
        timeframe = tomllib.loads((fam / "family.toml").read_text()).get("timeframe", "1h")
        for f in sorted(fam.glob("*.py")):
            # A fresh copy each time: the resolver writes the strategy's timeframe and exit into the config,
            # where they would override the next strategy's own.
            s = StrategyResolver.load_strategy({**base, "strategy_path": str(fam), "strategy": f.stem})
            assert s.timeframe == timeframe and s.can_short is False and s.stoploss < 0, f.stem


def test_no_lab_module_shadows_an_installed_one():
    """The jobs run `python /repo/lab/<job>.py`, which puts lab/ first on sys.path, and the image's
    PYTHONPATH=/repo puts the lab and web packages ahead of site-packages."""
    lab = {p.stem for p in LAB.glob("*.py")} | {p.name for p in LAB.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}
    repo = {"lab", "web"}
    installed = {m.name for m in pkgutil.iter_modules([p for p in sys.path if p and Path(p).resolve() not in (LAB, LAB.parent)])}
    assert (lab | repo) & (installed | set(sys.stdlib_module_names)) == set()


def synthetic_candles(start: dt.datetime, end: dt.datetime, freq: str, seed: int = 11) -> pd.DataFrame:
    date = pd.date_range(start, end, freq=freq, tz="UTC")
    rng = np.random.default_rng(seed)
    close = 30_000 * np.exp(np.cumsum(rng.normal(0, 0.012, len(date))))
    opn = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.004, len(date)))
    return pd.DataFrame({"date": date, "open": opn, "high": np.maximum(opn, close) * (1 + wick),
                         "low": np.minimum(opn, close) * (1 - wick), "close": close, "volume": 10.0})


@pytest.fixture
def bias_data(tmp_path, monkeypatch):
    from lab import analyze

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    data = tmp_path / "data" / analyze.BIAS_EXCHANGE
    data.mkdir(parents=True)
    # BTC plus two coins on other seeds, so a ratio to BTC (RatioRevert) and a ranking (XSMomentumRotation) move.
    for seed, coin in ((11, "BTC"), (12, "ETH"), (13, "SOL")):
        for tf, freq in (("1h", "h"), ("5m", "5min")):
            synthetic_candles(now - dt.timedelta(days=analyze.LOOKAHEAD_DAYS + 30), now, freq, seed).to_feather(data / f"{coin}_USD-{tf}.feather")
        # Daily candles reach far enough back for a 200-day startup before a 1d strategy's longer window, and for
        # the range family's BTC 200-day mean.
        day = now.replace(hour=0)
        synthetic_candles(day - dt.timedelta(days=DAILY_DAYS + 400), day - dt.timedelta(days=1), "D", seed).to_feather(data / f"{coin}_USD-1d.feather")
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    return analyze


DAILY_DAYS = 900  # a 1d strategy's lookahead window: 120 daily candles hold too few entries for the check


@pytest.mark.parametrize("strategy, folder, biased", [
    ("CandleENGULFING", CANDLES, False), ("ChartResistanceBreak", CHART, False),
    ("TrendDonchianFast", TREND, False), ("RangeZ24", RANGE, False),
    *[(f.stem, CATALOGUE, False) for f in sorted(CATALOGUE.glob("*.py"))],  # each one, as each is a trial
    ("PlantedLookahead", PLANTED, True),  # the control: the same check on the same data must catch it
])
def test_the_nightly_lookahead_check_on_synthetic_prices(bias_data, monkeypatch, strategy, folder, biased):
    if folder == TREND or strategy in ("TSMomentum", "XSMomentumRotation"):  # daily
        monkeypatch.setattr(bias_data, "LOOKAHEAD_DAYS", DAILY_DAYS)
    res = bias_data.bias_one("lookahead", strategy, str(folder))
    assert res.get("error") is None, res
    assert res["has_bias"] is biased and res["detail"]["signals"] > 0, res


def test_the_nightly_analysis_does_not_scan_the_staged_families():
    """Staged means no trials: the analysis searches user_data/strategies only. Switching a family on
    moves its folder there; the remaining staged families must stay outside discovery."""
    from lab import analyze

    staged = {p.stem for fam in generate.families() if fam.parent == FAMILIES for p in fam.glob("*.py")}
    assert staged and not staged & {s["name"] for s in analyze.Cli().discover()}


def test_active_candles_cover_every_bullish_talib_pattern():
    import talib
    from lab import analyze

    cfg = tomllib.loads((CANDLES / "family.toml").read_text())
    expected = {cfg["prefix"] + p.removeprefix("CDL") for p in talib.get_function_groups()["Pattern Recognition"]
                if p not in cfg["skip"]["bearish_only"]}
    assert {p.stem for p in CANDLES.glob("*.py")} == expected
    discovered = {s["name"]: s for s in analyze.Cli().discover()}
    assert expected <= discovered.keys()
    assert all(discovered[name]["load_error"] is None for name in expected)
    assert not (FAMILIES / "candles").exists()


def test_active_chart_members_are_discovered():
    from lab import analyze

    expected = {"ChartResistanceBreak", "ChartDoubleBottom", "ChartAscendingTriangle",
                "ChartSymmetricTriangle", "ChartBullFlag"}
    assert {p.stem for p in CHART.glob("*.py")} == expected
    discovered = {s["name"]: s for s in analyze.Cli().discover()}
    assert expected <= discovered.keys()
    assert all(discovered[name]["load_error"] is None for name in expected)
    assert not (FAMILIES / "chart").exists()


@pytest.mark.parametrize("family", [CANDLES, CHART], ids=lambda p: p.name)
def test_each_active_family_member_is_a_trial_even_with_no_trades(conn, monkeypatch, family):
    from lab import analyze, confidence

    names = {p.stem for p in family.glob("*.py")}
    assert names
    conn.execute("TRUNCATE trials")
    monkeypatch.setattr(analyze, "EXCHANGES", ["binanceus", "okx"])
    monkeypatch.setattr(analyze, "daily_close", lambda coin: pd.Series(dtype=float))
    for name in names:
        conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES (%s, %s, 'test')",
                     (name, str(family / f"{name}.py")))
        for venue in analyze.EXCHANGES:
            conn.execute(
                "INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok, "
                "bt_start, bt_end, trades, start_balance, final_balance) "
                "VALUES (%s, %s, '2026-01-01', '2026-02-01', 'test', true, "
                "'2026-01-01', '2026-02-01', 0, 1000, 1000)", (name, venue))
    assert analyze.derive(conn)["series"] == len(names) * len(analyze.EXCHANGES)
    assert conn.execute("SELECT max(trades) FROM strategy_windows").fetchone()[0] == 0
    expected = {(name, venue, "as-is") for name in names for venue in analyze.EXCHANGES}
    assert confidence.register(conn) == len(expected)
    assert set(conn.execute("SELECT strategy, exchange, variant FROM trials").fetchall()) == expected
    assert confidence.register(conn) == len(expected)
    conn.execute("DELETE FROM strategy_windows")
    conn.execute("DELETE FROM strategies")
    assert confidence.register(conn) == len(expected)
