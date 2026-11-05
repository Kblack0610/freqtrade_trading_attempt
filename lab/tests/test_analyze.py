import datetime as dt
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import analyze
import metrics

TODAY = dt.date(2026, 11, 4)
REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "strategies"


def strat(name: str, tf: str = "5m", load_error: str | None = None, rev: str = "1") -> dict:
    return {"name": name, "file": f"user_data/strategies/{name}.py", "file_hash": f"{name}-{rev}", "timeframe": tf,
            "startup_candle_count": 30, "load_error": load_error}


class FakeRunner:
    """Deterministic trades per strategy x exchange x day; `fail` names strategies whose backtests fail."""

    def __init__(self, found: list[dict], fail: set[str] = frozenset(), biased: set[str] = frozenset()):
        self.found, self.fail, self.biased = found, set(fail), set(biased)
        self.backtests: list[tuple] = []
        self.checks: list[tuple] = []

    def version(self) -> str:
        return "test"

    def discover(self) -> list[dict]:
        return self.found

    def backtest(self, strategy, exchange, start, end):
        self.backtests.append((strategy, exchange, start))
        if strategy in self.fail:
            return {"ok": False, "error": "boom", "duration_s": 0.1}
        rng = np.random.default_rng(metrics.seed_of(strategy, exchange, start))
        opens = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), freq="2D", inclusive="left")
        ratio = rng.normal(0.002, 0.01, len(opens))
        t = pd.DataFrame({"pair": "BTC/USD", "open_date": opens, "close_date": opens + pd.Timedelta(hours=8),
                          "open_rate": 100.0, "close_rate": 100.0 * (1 + ratio), "stake_amount": 300.0,
                          "profit_ratio": ratio, "profit_abs": 300.0 * ratio, "exit_reason": "roi", "enter_tag": None})
        return {"ok": True, "error": None, "duration_s": 0.1, "trades": t, "bt_start": pd.Timestamp(start, tz="UTC"),
                "bt_end": pd.Timestamp(end, tz="UTC"), "start_balance": 1000.0,
                "final_balance": 1000.0 + float(t["profit_abs"].sum()), "max_drawdown": 0.05}

    def bias(self, kind, strategy):
        self.checks.append((kind, strategy))
        return {"has_bias": strategy in self.biased and kind == "lookahead",
                "biased_indicators": "next_close" if strategy in self.biased else None, "detail": {"signals": 40}}


@pytest.fixture
def lab(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(analyze, "USER_DATA", REPO / "user_data")
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    monkeypatch.setattr(analyze, "EXCHANGES", ["binanceus", "coinbase"])
    monkeypatch.setattr(analyze, "ONLY", set())
    monkeypatch.setattr(analyze, "WORKERS", 3)
    for ex, first in (("binanceus", "2024-03-01"), ("coinbase", "2025-01-01")):
        conn.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
                     "VALUES (%s, 'BTC/USD', '5m', %s, now(), 1, true)", (ex, first))
    days = pd.date_range("2023-01-01", "2026-11-03", freq="D", tz="UTC")
    (tmp_path / "data" / "coinbase").mkdir(parents=True)
    pd.DataFrame({"date": days, "close": np.linspace(20000, 90000, len(days))}).to_feather(
        tmp_path / "data" / "coinbase" / "BTC_USD-1d.feather")
    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('binanceus-good', 'binanceus', 'Good')")
    return conn


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_full_history_in_yearly_chunks_then_cached(lab):
    found = [strat("Good"), strat("Other"), strat("Planted"), strat("Scalper", "1m"), strat("Broken", None, "failed to import")]
    r = FakeRunner(found, biased={"Planted"})
    assert analyze.main(r, TODAY) == 0
    # binanceus 2024, 2025, 2026 and coinbase 2025, 2026, for each of the three 5m strategies
    assert len(r.backtests) == 15 and ("Good", "binanceus", dt.date(2024, 1, 1)) in r.backtests
    assert len(r.checks) == 6
    assert one(lab, "SELECT count(*) FROM backtest_chunks WHERE ok") == 15
    assert one(lab, "SELECT chunk_end FROM backtest_chunks WHERE chunk_start = '2026-01-01' LIMIT 1") == TODAY
    assert one(lab, "SELECT count(*) FROM backtest_trades") > 0
    raw = one(lab, "SELECT raw FROM runs WHERE kind = 'analysis'")
    assert raw["chunks_run"] == 15 and raw["load_failed"] == 1
    assert sorted(raw["skipped"]) == ["Scalper on binanceus: no 1m candles", "Scalper on coinbase: no 1m candles"]
    assert one(lab, "SELECT bool_and(ok AND finished_at IS NOT NULL) FROM runs")

    # 2024-03-01 to today is more than 365 days on binanceus; coinbase starts 2025-01-01, so also yes
    assert one(lab, "SELECT count(*) FROM strategy_windows WHERE win = '365d'") == 6
    assert one(lab, "SELECT count(*) FROM strategy_windows WHERE win = 'all' AND strategy = 'Good'") == 2
    assert one(lab, "SELECT min(trials) = max(trials) AND min(trials) = 6 FROM strategy_windows WHERE win = '365d'")
    assert one(lab, "SELECT bool_and(dsr BETWEEN 0 AND 1) FROM strategy_windows WHERE dsr IS NOT NULL")
    assert one(lab, "SELECT count(*) FROM estimates") == 12
    assert one(lab, "SELECT count(*) FROM regimes WHERE trend = 'bull'") > 0
    assert one(lab, "SELECT count(*) FROM correlations WHERE other = 'Good' AND exchange = 'binanceus'") == 2
    assert one(lab, "SELECT has_bias FROM bias_checks WHERE strategy = 'Planted' AND kind = 'lookahead'") is True
    days = one(lab, "SELECT count(*) FROM strategy_daily WHERE strategy = 'Good' AND exchange = 'binanceus'")
    assert days == (TODAY - dt.date(2024, 1, 1)).days

    before = lab.execute("SELECT strategy, exchange, horizon_days, p10, p50, p90 FROM estimates ORDER BY 1, 2, 3").fetchall()
    r2 = FakeRunner(found, biased={"Planted"})
    assert analyze.main(r2, TODAY) == 0
    assert r2.backtests == [] and r2.checks == []
    assert lab.execute("SELECT strategy, exchange, horizon_days, p10, p50, p90 FROM estimates ORDER BY 1, 2, 3").fetchall() == before

    r3 = FakeRunner(found, biased={"Planted"})
    assert analyze.main(r3, TODAY + dt.timedelta(days=1)) == 0
    assert sorted({s for s, _, _ in r3.backtests}) == ["Good", "Other", "Planted"]
    assert {start for _, _, start in r3.backtests} == {dt.date(2026, 1, 1)}


def test_a_changed_file_reruns_its_chunks_and_bias(lab):
    assert analyze.main(FakeRunner([strat("Good")]), TODAY) == 0
    r = FakeRunner([strat("Good", rev="2")])
    assert analyze.main(r, TODAY) == 0
    assert len(r.backtests) == 5 and len(r.checks) == 2


def test_failed_chunks_retry_three_times_and_fail_the_run(lab):
    found = [strat("Good"), strat("Flaky")]
    codes, calls = [], []
    for _ in range(4):
        r = FakeRunner(found, fail={"Flaky"})
        codes.append(analyze.main(r, TODAY))
        calls.append(len(r.backtests))
    assert calls == [10, 5, 5, 0] and codes == [1, 1, 1, 0]
    assert one(lab, "SELECT max(attempts) FROM backtest_chunks WHERE strategy = 'Flaky'") == 3
    assert one(lab, "SELECT count(*) FROM backtest_chunks WHERE strategy = 'Flaky' AND ok") == 0
    assert one(lab, "SELECT count(*) FROM strategy_windows WHERE strategy = 'Flaky'") == 0


def test_a_deleted_strategy_takes_its_results(lab):
    analyze.main(FakeRunner([strat("Good"), strat("Gone")]), TODAY)
    assert analyze.main(FakeRunner([strat("Good")]), TODAY) == 0
    for table in ("backtest_chunks", "strategy_windows", "strategy_daily", "estimates", "bias_checks"):
        assert one(lab, f"SELECT count(*) FROM {table} WHERE strategy = 'Gone'") == 0, table
    assert one(lab, "SELECT count(*) FROM strategies WHERE name = 'Gone'") == 0
    assert one(lab, "SELECT count(*) FROM backtest_trades t LEFT JOIN backtest_chunks c ON c.id = t.chunk_id "
                    "WHERE c.id IS NULL") == 0


def test_strategy_filter_limits_the_run(lab, monkeypatch):
    monkeypatch.setattr(analyze, "ONLY", {"Good"})
    r = FakeRunner([strat("Good"), strat("Other")])
    assert analyze.main(r, TODAY) == 0
    assert {s for s, _, _ in r.backtests} == {"Good"} and {s for _, s in r.checks} == {"Good"}


def test_chunks_are_calendar_years_to_today():
    assert analyze.chunks(dt.date(2024, 6, 3), TODAY) == [
        (dt.date(2024, 1, 1), dt.date(2025, 1, 1)), (dt.date(2025, 1, 1), dt.date(2026, 1, 1)),
        (dt.date(2026, 1, 1), TODAY)]
    assert analyze.chunks(dt.date(2026, 1, 1), dt.date(2026, 1, 1)) == []


DATA = Path(os.environ.get("LAB_RESULTS", "/lab")) / "data" / "binanceus" / "BTC_USD-5m.feather"


@pytest.mark.skipif(not DATA.exists(), reason="needs backfilled binanceus data under LAB_RESULTS")
def test_planted_lookahead_is_flagged_and_a_clean_strategy_is_not():
    planted = analyze.bias_one("lookahead", "PlantedLookahead", str(FIXTURES))
    assert planted["has_bias"] is True and "next_close" in planted["biased_indicators"]
    assert analyze.bias_one("lookahead", "Strategy001", None)["has_bias"] is False
