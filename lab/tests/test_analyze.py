import datetime as dt
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import analyze
import metrics

TODAY = dt.date(2026, 11, 4)
REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "strategies"


@pytest.mark.parametrize("path", sorted((REPO / "user_data" / "exchanges").glob("*.json")), ids=lambda p: p.stem)
def test_every_exchange_sets_its_fee(path):
    """Without one, backtests and dry runs use ccxt's default fee, not the venue's schedule."""
    assert 0 < json.loads(path.read_text())["fee"] < 0.1


def strat(name: str, tf: str = "5m", load_error: str | None = None, rev: str = "1") -> dict:
    return {"name": name, "file": f"user_data/strategies/{name}.py", "file_hash": f"{name}-{rev}", "timeframe": tf,
            "startup_candle_count": 30, "load_error": load_error}


class FakeRunner:
    """Deterministic trades per strategy x exchange x day; `fail` names strategies whose backtests fail."""

    def __init__(self, found: list[dict], fail: set[str] = frozenset(), biased: set[str] = frozenset(),
                 hole: set[tuple[str, int]] = frozenset()):
        self.found, self.fail, self.biased, self.hole = found, set(fail), set(biased), set(hole)
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
        if (exchange, start.year) in self.hole:
            return {"ok": False, "no_data": True, "error": analyze.NO_DATA, "duration_s": 0.1}
        rng = np.random.default_rng(metrics.seed_of(strategy, exchange, start))
        opens = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), freq="2D", inclusive="left")
        ratio = rng.normal(0.002, 0.01, len(opens))
        t = pd.DataFrame({"pair": "BTC/USD", "open_date": opens, "close_date": opens + pd.Timedelta(hours=8),
                          "open_rate": 100.0, "close_rate": 100.0 * (1 + ratio), "min_rate": 95.0, "stake_amount": 300.0,
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
    # every trade stakes 300 of a 1000 wallet for 8 hours in 48, so 5% of capital is in the market
    assert one(lab, "SELECT bool_and(abs(capital_exposure - 0.05) < 0.002) FROM strategy_windows WHERE win = '365d'")
    assert one(lab, "SELECT bool_and(hold_excess_lo < hold_excess AND hold_excess < hold_excess_hi) FROM strategy_windows")
    assert one(lab, "SELECT bool_and(abs(worst_open + 0.05) < 1e-9) FROM strategy_windows WHERE trades > 0")
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


def test_each_league_bot_is_set_against_a_backtest_of_its_own_period(lab):
    lab.execute("UPDATE bots SET first_seen = '2026-10-20 06:00+00' WHERE name = 'binanceus-good'")
    for i, (opened, is_open, ratio) in enumerate((("2026-10-22 00:02+00", False, 0.004), ("2026-10-25 12:00+00", False, -0.01),
                                                  ("2026-11-04 00:00+00", True, None))):
        lab.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, profit_ratio, profit_abs, raw) "
                    "VALUES ('binanceus-good', %s, %s, 'BTC/USD', %s, %s, %s, '{}')",
                    (i + 1, opened, is_open, ratio, None if ratio is None else 300 * ratio))
    r = FakeRunner([strat("Good")])
    assert analyze.main(r, TODAY) == 0
    assert ("Good", "binanceus", dt.date(2026, 10, 20)) in r.backtests
    row = lab.execute("SELECT live_entries, bt_entries, matched, live_closed, gap_trades, error FROM drift "
                      "WHERE bot = 'binanceus-good'").fetchone()
    # the backtest enters every other day from 2026-10-20; its 10-20 entry is before the bot's start
    assert row == (3, 7, 1, 2, 1, None)
    assert one(lab, "SELECT raw->>'drift_run' FROM runs ORDER BY id DESC LIMIT 1") == "1"


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


def test_a_year_with_no_candles_is_stored_once_and_is_no_failure(lab):
    found = [strat("Good")]
    r = FakeRunner(found, hole={("binanceus", 2025)})
    assert analyze.main(r, TODAY) == 0
    assert one(lab, "SELECT attempts FROM backtest_chunks WHERE exchange = 'binanceus' AND chunk_start = '2025-01-01'") == 3
    r = FakeRunner(found, hole={("binanceus", 2025)})
    assert analyze.main(r, TODAY) == 0
    assert ("Good", "binanceus", dt.date(2025, 1, 1)) not in r.backtests
    assert one(lab, "SELECT count(*) FROM strategy_windows WHERE strategy = 'Good' AND exchange = 'binanceus'") > 0


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


@pytest.mark.skipif(not DATA.exists(), reason="needs backfilled binanceus data under LAB_RESULTS")
def test_a_strategy_in_a_subfolder_backtests_and_checks(monkeypatch):
    monkeypatch.setattr(analyze, "EXCHANGES", ["binanceus"])
    r = analyze.Cli().backtest("BbandRsi", "binanceus", dt.date(2025, 9, 1), dt.date(2025, 11, 1))
    assert r["ok"], r["error"]
    res = analyze.bias_one("lookahead", "BbandRsi", None)
    assert "Impossible to load" not in (res.get("error") or ""), res


def test_a_class_freqtrade_cannot_find_by_name_is_a_load_error():
    assert analyze.load_error("class Foo(IStrategy):", "Foo") is None
    assert "no space" in analyze.load_error("class Foo (IStrategy):", "Foo")
    found = {s["name"]: s for s in analyze.Cli().discover()}
    assert found["multi_tf"]["load_error"] is None


def test_a_non_finite_number_in_a_check_is_stored_as_null(lab):
    class Nan(FakeRunner):
        def bias(self, kind, strategy):
            return {"has_bias": False, "detail": {"diff": {"rsi": {"30": float("nan"), "60": float("inf"), "90": 0.0}}}}

    assert analyze.main(Nan([strat("Good")]), TODAY) == 0
    assert one(lab, "SELECT detail->'diff'->'rsi' FROM bias_checks WHERE strategy = 'Good' AND kind = 'recursive'") == \
        {"30": None, "60": None, "90": 0.0}


def test_a_store_error_ends_the_run_without_running_the_queue(lab, monkeypatch):
    """One worker: the first check's store raises, so of the five chunks queued at most the one already running runs."""
    cancelled = threading.Event()

    class Pool(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            super().shutdown(wait=False, cancel_futures=cancel_futures)
            if cancel_futures:
                cancelled.set()
            super().shutdown(wait=wait)

    class Held(FakeRunner):
        def backtest(self, strategy, exchange, start, end):
            cancelled.wait(10)
            return super().backtest(strategy, exchange, start, end)

    def store_bias(*_):
        raise RuntimeError("cannot store")

    monkeypatch.setattr(analyze, "WORKERS", 1)
    monkeypatch.setattr(analyze, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(analyze, "store_bias", store_bias)
    r = Held([strat("Good")])
    with pytest.raises(RuntimeError, match="cannot store"):
        analyze.main(r, TODAY)
    assert len(r.backtests) <= 1
    assert one(lab, "SELECT NOT ok AND raw->>'error' = 'RuntimeError: cannot store' FROM runs WHERE kind = 'analysis'")


def test_errors_keep_an_exception_message_whole():
    out = ("2026-11-05 03:17:02,455 - freqtrade - ERROR - Fatal exception!\n"
           "Traceback (most recent call last):\n"
           "ValueError: Seed must be between 0 and 2**32 - 1\n")
    assert analyze._errors(out) == "Fatal exception! | ValueError: Seed must be between 0 and 2**32 - 1"


def test_task_memory_matches_the_measured_peaks(monkeypatch):
    monkeypatch.setattr(analyze, "pair_count", lambda ex: 10)
    assert analyze.task_gb("binanceus", "5m", 365) == pytest.approx(1.1, abs=0.1)
    assert analyze.task_gb("binanceus", "1m", 365) == pytest.approx(4.0, abs=0.1)


def test_the_budget_holds_a_task_back_until_memory_frees():
    b = analyze.Budget(10)
    entered = threading.Event()

    def second():
        with b.hold(6):
            entered.set()

    with b.hold(6):
        t = threading.Thread(target=second)
        t.start()
        with b.cv:
            assert b.cv.wait_for(lambda: b.waiting == 1, timeout=10)  # guard only; the notify ends the wait
        assert not entered.is_set() and b.used == 6
    t.join(timeout=10)
    assert entered.is_set() and b.used == 0


def test_a_task_above_the_budget_runs_alone():
    b = analyze.Budget(10)
    with b.hold(25):
        assert b.used == 10
    assert b.used == 0
