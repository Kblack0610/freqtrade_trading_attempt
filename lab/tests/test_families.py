"""The staged strategy families in lab/families/: generated files current, every strategy loadable by
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
CANDLES = FAMILIES / "candles"
CHART = FAMILIES / "chart"
PLANTED = LAB / "tests" / "fixtures" / "strategies"


def test_every_family_file_is_what_the_generator_writes():
    assert {f.name for f in generate.families()} >= {"candles", "chart"}
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

    cfg = Configuration.from_files([str(p) for p in analyze.config_files(analyze.BIAS_EXCHANGE)])
    for fam in generate.families():
        cfg["strategy_path"] = str(fam)
        for f in sorted(fam.glob("*.py")):
            cfg["strategy"] = f.stem
            s = StrategyResolver.load_strategy(cfg)
            assert s.timeframe == "1h" and s.can_short is False and s.stoploss < 0, f.stem


def test_no_lab_module_shadows_an_installed_one():
    """The jobs run `python /repo/lab/<job>.py`, which puts lab/ first on sys.path, and the image's
    PYTHONPATH=/repo puts the lab and web packages ahead of site-packages."""
    lab = {p.stem for p in LAB.glob("*.py")} | {p.name for p in LAB.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}
    repo = {"lab", "web"}
    installed = {m.name for m in pkgutil.iter_modules([p for p in sys.path if p and Path(p).resolve() not in (LAB, LAB.parent)])}
    assert (lab | repo) & (installed | set(sys.stdlib_module_names)) == set()


def synthetic_candles(start: dt.datetime, end: dt.datetime, freq: str) -> pd.DataFrame:
    date = pd.date_range(start, end, freq=freq, tz="UTC")
    rng = np.random.default_rng(11)
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
    for tf, freq in (("1h", "h"), ("5m", "5min")):
        synthetic_candles(now - dt.timedelta(days=analyze.LOOKAHEAD_DAYS + 30), now, freq).to_feather(data / f"BTC_USD-{tf}.feather")
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    return analyze


@pytest.mark.parametrize("strategy, folder, biased", [
    ("CandleENGULFING", CANDLES, False), ("ChartResistanceBreak", CHART, False),
    ("PlantedLookahead", PLANTED, True),  # the control: the same check on the same data must catch it
])
def test_the_nightly_lookahead_check_on_synthetic_prices(bias_data, strategy, folder, biased):
    res = bias_data.bias_one("lookahead", strategy, str(folder))
    assert res.get("error") is None, res
    assert res["has_bias"] is biased and res["detail"]["signals"] > 0, res


def test_the_nightly_analysis_does_not_scan_the_staged_families():
    """Staged means no trials: the analysis searches user_data/strategies only. Switching a family on
    moves its folder there and drops this test."""
    from lab import analyze

    staged = {p.stem for fam in generate.families() for p in fam.glob("*.py")}
    assert staged and not staged & {s["name"] for s in analyze.Cli().discover()}
