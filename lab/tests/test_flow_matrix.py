"""lab/flow_matrix.py: every strategy of the book through the backtest, bias and book avenues on synthetic candles.
The unit tests need no freqtrade. The matrix itself (the sample, the controls, the isolation check) runs real
backtests and bias checks, about four minutes, so CI runs it in the test-calibration job with FLOW_E2E=1."""

import datetime as dt
import os
import shutil
from pathlib import Path

import pytest

from lab import flow_matrix as fm
from lab import suggest

CFG = suggest.load_config()
FIXTURES = Path(__file__).parent / "fixtures" / "strategies"
CRASHES = '''
from freqtrade.strategy import IStrategy


class FlowCrash(IStrategy):
    """Negative control: fails inside populate_indicators, so its backtest cell must fail."""
    timeframe = "1h"
    startup_candle_count = 5
    stoploss = -0.1
    minimal_roi = {"0": 0.1}

    def populate_indicators(self, dataframe, metadata):
        raise RuntimeError("planted crash")

    def populate_entry_trend(self, dataframe, metadata):
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe
'''


def strat(name, family, load_error=None):
    return {"name": name, "file": f"user_data/strategies/{name}.py", "family": family, "load_error": load_error}


def test_the_sample_is_one_runnable_strategy_per_family():
    found = [strat("A1", "trend"), strat("A2", "trend"), strat("B", "grid"), strat("C", "ml"), strat("D", "short"),
             strat("E", "flow", "does not load"), strat("F", "flow")]
    assert [s["name"] for s in fm.sample(found)] == ["A1", "B", "F"]


def test_every_family_of_the_book_has_a_strategy_the_sample_can_run():
    import tomllib
    toml = tomllib.loads((fm.REPO / "lab" / "families.toml").read_text())
    runnable = {v["family"] for v in toml["strategies"].values()} - fm.OUT_OF_BOOK
    assert runnable and runnable <= set(toml["families"])


def test_monthly_returns_are_profit_over_the_start_balance_per_calendar_month():
    import pandas as pd
    t = pd.DataFrame({"close_date": pd.to_datetime(["2026-01-05", "2026-01-20", "2026-03-02"], utc=True),
                      "profit_abs": [10.0, -4.0, 20.0]})
    got = fm.monthly_returns(t, 1000.0)
    assert [(a.isoformat(), b.isoformat(), round(v, 4)) for a, b, v in got] == [
        ("2026-01-01", "2026-02-01", 0.006), ("2026-03-01", "2026-04-01", 0.02)]
    assert fm.monthly_returns(t.iloc[:0], 1000.0) == []


def months(n, v=0.01):
    return [[f"2025-{m:02d}-01", f"2025-{m + 1:02d}-01", v if m % 2 else -v / 2] for m in range(1, n + 1)]


def test_book_cell_projects_enough_months_skips_too_few_and_skips_a_failed_backtest():
    import pandas as pd
    close = pd.Series(range(1, 400), index=pd.date_range("2024-12-01", periods=399, freq="D", tz="UTC"), dtype=float)
    world = {"closes": {"BTC/USD": close}}
    ok = fm.book_cell({"status": "pass", "months": months(9)}, world, CFG)
    assert ok["status"] == "pass", ok
    few = fm.book_cell({"status": "pass", "months": months(5)}, world, CFG)
    assert few["status"] == "skip" and "min_folds" in few["detail"]
    assert fm.book_cell({"status": "fail", "detail": "x"}, world, CFG)["status"] == "skip"


def test_the_summary_counts_each_avenue():
    cells = [{"avenue": "backtest", "status": "pass"}, {"avenue": "bias", "status": "fail"},
             {"avenue": "bias", "status": "skip"}, {"avenue": "book", "status": "pass"}]
    assert fm.summary(cells) == {"backtest": {"pass": 1, "fail": 0, "skip": 0}, "bias": {"pass": 0, "fail": 1, "skip": 1},
                                 "book": {"pass": 1, "fail": 0, "skip": 0}}


def test_the_bias_check_with_nothing_to_judge_is_not_a_failure():
    assert fm.NOTHING_TO_JUDGE.search("only 0 signals in 20 days")
    assert fm.NOTHING_TO_JUDGE.search("ConfigurationError: The strategy defines invalid startup candle count of 0.")
    assert not fm.NOTHING_TO_JUDGE.search("OperationalException: No data found. Terminating.")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if not os.environ.get("FLOW_E2E"):
        pytest.skip("FLOW_E2E not set")
    pytest.importorskip("freqtrade")
    root = tmp_path_factory.mktemp("flow")
    w = fm.write_world(root, dt.datetime.now(dt.timezone.utc).date())
    shutil.copy(FIXTURES / "PlantedLookahead.py", w["repo"] / "user_data" / "strategies")
    (w["repo"] / "user_data" / "strategies" / "FlowCrash.py").write_text(CRASHES)
    return w


def test_the_sample_runs_every_avenue_without_a_failed_cell(world):
    before = sorted(p.relative_to(fm.REPO) for p in (fm.REPO / "user_data").rglob("*") if p.is_file())
    cells = fm.run(fm.sample(fm.strategies()), world)
    failed = [c for c in cells if c["status"] == "fail"]
    assert not failed, failed
    s = fm.summary(cells)
    assert all(s[a]["pass"] > 0 for a in fm.AVENUES), s
    # isolation: the real user_data gained no file (no results, data or params) and the world sits in the temp dir
    assert before == sorted(p.relative_to(fm.REPO) for p in (fm.REPO / "user_data").rglob("*") if p.is_file())
    assert str(world["repo"]).startswith(str(world["results"].parent)) and fm.REPO not in world["repo"].parents


def test_a_crashing_strategy_fails_its_backtest_cell(world):
    cell = fm.run_cell(world, "backtest", "FlowCrash")
    assert cell["status"] == "fail" and "planted crash" in cell["detail"], cell


def test_a_strategy_that_reads_the_next_candle_is_flagged_by_the_bias_cell(world):
    cell = fm.run_cell(world, "bias", "PlantedLookahead")
    assert cell["status"] == "pass" and cell["has_bias"].get("lookahead") is True, cell
