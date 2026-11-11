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
TREND = ACTIVE / "trend"
RANGE = ACTIVE / "range"
BREAKOUT = ACTIVE / "breakout"
CLOCK = ACTIVE / "clock"
FLOW = ACTIVE / "flow"
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
        timeframe = {name: tf for name, _, _, tf, _ in generate.rungs(tomllib.loads((fam / "family.toml").read_text()))}
        assert {f.stem for f in fam.glob("*.py")} == timeframe.keys(), fam.name
        for name, tf in sorted(timeframe.items()):
            # A fresh copy each time: the resolver writes the strategy's timeframe and exit into the config,
            # where they would override the next strategy's own.
            s = StrategyResolver.load_strategy({**base, "strategy_path": str(fam), "strategy": name})
            assert s.timeframe == tf and s.can_short is False and -1 < s.stoploss < 0, name


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
                         "low": np.minimum(opn, close) * (1 - wick), "close": close,
                         "volume": 10.0 * np.exp(rng.normal(0, 0.8, len(date)))})  # varies, so volume signals fire


@pytest.fixture
def bias_data(tmp_path, monkeypatch):
    from lab import analyze

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    data = tmp_path / "data" / analyze.BIAS_EXCHANGE
    data.mkdir(parents=True)
    # BTC plus two coins on other seeds, so a ratio to BTC (RatioRevert) and a ranking (XSMomentumRotation) move.
    # Each timeframe reaches back over its lookahead window, a 200-candle startup and, for 1d, the range family's
    # BTC 200-day mean; each ends at its last closed candle.
    for seed, coin in ((11, "BTC"), (12, "ETH"), (13, "SOL")):
        for tf, freq in (("5m", "5min"), ("15m", "15min"), ("1h", "h"), ("4h", "4h"), ("1d", "D")):
            step = pd.tseries.frequencies.to_offset(freq)
            end = pd.Timestamp(now).floor(freq) - step
            start = (end - pd.Timedelta(days=analyze.lookahead_days(tf) + 30) - 400 * step).floor(freq)
            synthetic_candles(start, end, freq, seed).to_feather(data / f"{coin}_USD-{tf}.feather")
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    return analyze


@pytest.mark.parametrize("strategy, folder, biased", [
    # One of each family, across the rungs: every member of a family shares its template and signal module.
    ("CandleENGULFING", CANDLES, False), ("ChartResistanceBreak", CHART, False),
    ("TrendDonchianFast", TREND, False), ("TrendDonchian_4h", TREND, False), ("RangeZ24", RANGE, False),
    ("BreakoutAtr", BREAKOUT, False), ("BreakoutSqueeze_15m", BREAKOUT, False),
    ("ClockEvening_15m", CLOCK, False), ("ClockWeekend_1d", CLOCK, False),
    ("FlowSurge_15m", FLOW, False), ("FlowObv", FLOW, False),
    *[(f.stem, CATALOGUE, False) for f in sorted(CATALOGUE.glob("*.py"))],  # each one, as each is a trial
    ("PlantedLookahead", PLANTED, True),  # the control: the same check on the same data must catch it
])
def test_the_nightly_lookahead_check_on_synthetic_prices(bias_data, strategy, folder, biased):
    res = bias_data.bias_one("lookahead", strategy, str(folder))
    assert res.get("error") is None, res
    assert res["has_bias"] is biased and res["detail"]["signals"] > 0, res


def test_a_slow_timeframe_gets_a_lookahead_window_of_enough_candles():
    from lab import analyze

    assert [analyze.lookahead_days(tf) for tf in ("5m", "1h", "4h", "1d")] == [120, 120, 150, 900]


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


ACTIVE_FAMILIES = [f for f in generate.families() if f.parent == ACTIVE]


def test_every_active_family_member_is_discovered():
    """Switched on means scanned: the nightly analysis finds every rung of every family under user_data/strategies."""
    from lab import analyze

    assert {f.name for f in ACTIVE_FAMILIES} >= {"candles", "chart", "trend", "range", "breakout", "clock", "flow"}
    discovered = {s["name"]: s for s in analyze.Cli().discover()}
    for fam in ACTIVE_FAMILIES:
        expected = {p.stem for p in fam.glob("*.py")}
        assert expected <= discovered.keys(), fam.name
        assert all(discovered[name]["load_error"] is None for name in expected), fam.name
        assert not (FAMILIES / fam.name).exists()


@pytest.mark.parametrize("family", ACTIVE_FAMILIES, ids=lambda p: p.name)
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
