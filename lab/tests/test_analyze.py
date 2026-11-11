import datetime as dt
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab import analyze, metrics, variants

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

    def backtest(self, strategy, exchange, start, end, overlay=None):
        self.backtests.append((strategy, exchange, start) + ((overlay,) if overlay else ()))
        if strategy in self.fail:
            return {"ok": False, "error": "boom", "duration_s": 0.1}
        if (exchange, start.year) in self.hole:
            return {"ok": False, "no_data": True, "error": analyze.NO_DATA, "duration_s": 0.1}
        rng = np.random.default_rng(metrics.seed_of(strategy, exchange, start, *([json.dumps(overlay)] if overlay else [])))
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
    (tmp_path / "variants.toml").write_text("")
    monkeypatch.setattr(variants, "PATH", tmp_path / "variants.toml")
    for ex, first in (("binanceus", "2024-03-01"), ("coinbase", "2025-01-01")):
        for i, pair in enumerate(sorted(analyze.whitelist(ex))):
            conn.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
                         "VALUES (%s, %s, '5m', %s::timestamptz + %s * interval '1 day', now(), 1, true)", (ex, pair, first, i))
    days = pd.date_range("2023-01-01", "2026-11-03", freq="D", tz="UTC")
    (tmp_path / "data" / "coinbase").mkdir(parents=True)
    pd.DataFrame({"date": days, "close": np.linspace(20000, 90000, len(days))}).to_feather(
        tmp_path / "data" / "coinbase" / "BTC_USD-1d.feather")
    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('binanceus-good', 'binanceus', 'Good')")
    return conn


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_a_venue_is_planned_only_once_every_pair_reached_its_oldest_candle(lab):
    assert set(analyze.coverage(lab)) == {("binanceus", "5m"), ("coinbase", "5m")}
    assert analyze.coverage(lab)["binanceus", "5m"].date() == dt.date(2024, 3, 1)
    lab.execute("UPDATE data_coverage SET oldest_reached = false WHERE exchange = 'binanceus' AND pair = 'ETH/USD'")
    assert set(analyze.coverage(lab)) == {("coinbase", "5m")}
    lab.execute("DELETE FROM data_coverage WHERE exchange = 'coinbase' AND pair = 'ETH/USD'")
    assert analyze.coverage(lab) == {}


def test_full_history_in_yearly_chunks_then_cached(lab):
    found = [strat("Good"), strat("Other"), strat("Planted"), strat("Scalper", "1m"), strat("Broken", None, "failed to import")]
    r = FakeRunner(found, biased={"Planted"})
    assert analyze.main(r, TODAY) == 0
    # binanceus 2024, 2025 and coinbase 2025, then 11 months of 2026 on each, for each of the three 5m strategies
    assert len(r.backtests) == 75 and ("Good", "binanceus", dt.date(2024, 1, 1)) in r.backtests
    assert len(r.checks) == 6
    assert one(lab, "SELECT count(*) FROM backtest_chunks WHERE ok") == 75
    assert one(lab, "SELECT chunk_end FROM backtest_chunks WHERE chunk_start = '2026-11-01' LIMIT 1") == TODAY
    assert one(lab, "SELECT count(*) FROM backtest_trades") > 0
    raw = one(lab, "SELECT raw FROM runs WHERE kind = 'analysis'")
    assert raw["chunks_run"] == 75 and raw["load_failed"] == 1
    assert {"plan", "backtests", "derive", "confidence", "loop"} <= set(raw["stage_s"])
    assert sorted(raw["universe"]) == ["binanceus", "coinbase"]
    assert all(u["source"] == "current-listing" and u["pairs"] > 0 for u in raw["universe"].values())
    assert sorted(raw["skipped"]) == ["Scalper on binanceus: 1m not fully backfilled", "Scalper on coinbase: 1m not fully backfilled"]
    assert one(lab, "SELECT bool_and(ok AND finished_at IS NOT NULL) FROM runs")

    # 2024-03-01 to today is more than 365 days on binanceus; coinbase starts 2025-01-01, so also yes
    assert one(lab, "SELECT count(*) FROM strategy_windows WHERE win = '365d'") == 6
    assert one(lab, "SELECT count(*) FROM strategy_windows WHERE win = 'all' AND strategy = 'Good'") == 2
    assert one(lab, "SELECT min(trials_raw) = max(trials_raw) AND min(trials_raw) = 6 FROM strategy_windows WHERE win = '365d'")
    assert one(lab, "SELECT min(trials) = max(trials) AND min(trials) BETWEEN 1 AND 6 FROM strategy_windows WHERE win = '365d'")
    assert one(lab, "SELECT count(*) FROM overfit WHERE exchange = 'binanceus' AND win = '365d' AND pbo BETWEEN 0 AND 1") == 1
    assert one(lab, "SELECT bool_and(dsr BETWEEN 0 AND 1) FROM strategy_windows WHERE dsr IS NOT NULL")
    # every trade stakes 300 of a 1000 wallet for 8 hours in 48, so 5% of capital is in the market
    assert one(lab, "SELECT bool_and(abs(capital_exposure - 0.05) < 0.002) FROM strategy_windows WHERE win = '365d'")
    assert one(lab, "SELECT bool_and(hold_excess_lo < hold_excess AND hold_excess < hold_excess_hi) FROM strategy_windows")
    assert one(lab, "SELECT bool_and(abs(worst_open + 0.05) < 1e-9) FROM strategy_windows WHERE trades > 0")
    assert one(lab, "SELECT count(*) FROM estimates") == 12
    assert one(lab, "SELECT count(*) FROM regimes WHERE trend = 'up'") > 0
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
    assert {start for _, _, start in r3.backtests} == {dt.date(2026, 11, 1)} and len(r3.backtests) == 6


def test_the_current_month_reruns_and_a_finished_year_replaces_its_months(lab):
    """The first night over a stored year-to-date chunk, a month end, and January 1: no day is ever counted twice."""
    found = [strat("Good")]
    lab.execute("DELETE FROM bots")  # no drift backtest in the nights counted

    def series(today):
        days = one(lab, "SELECT count(*) FROM strategy_daily WHERE strategy = 'Good' AND exchange = 'binanceus'")
        assert days == (today - dt.date(2024, 1, 1)).days
        return lab.execute("SELECT chunk_start, chunk_end FROM backtest_chunks WHERE exchange = 'binanceus' "
                           "ORDER BY chunk_start").fetchall()

    # a year-to-date chunk stored before months: the month starting with it replaces it, the others overlap it
    lab.execute("INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok) "
                "VALUES ('Good', 'binanceus', '2026-01-01', '2026-10-30', 'old', true)")
    assert analyze.main(FakeRunner(found), TODAY) == 0
    assert len(series(TODAY)) == 13
    r = FakeRunner(found)
    assert analyze.main(r, dt.date(2026, 12, 1)) == 0
    assert sorted(r.backtests) == [("Good", ex, dt.date(2026, 11, 1)) for ex in ("binanceus", "coinbase")]
    assert series(dt.date(2026, 12, 1))[-1] == (dt.date(2026, 11, 1), dt.date(2026, 12, 1))
    r = FakeRunner(found)
    assert analyze.main(r, dt.date(2027, 1, 2)) == 0
    assert ("Good", "binanceus", dt.date(2026, 1, 1)) in r.backtests and len(r.backtests) == 4
    assert series(dt.date(2027, 1, 2)) == [(dt.date(y, 1, 1), dt.date(y + 1, 1, 1)) for y in (2024, 2025, 2026)] + [
        (dt.date(2027, 1, 1), dt.date(2027, 1, 2))]


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
    assert len(r.backtests) == 25 and len(r.checks) == 2


def test_candles_added_to_a_past_year_rerun_only_that_year(lab):
    """Kraken's quarterly archive lands after its year ends, when that year's range no longer changes."""
    path = analyze.RESULTS / "data" / "binanceus" / "BTC_USD-5m.feather"
    path.parent.mkdir(parents=True)
    days = pd.date_range("2024-03-01", "2026-11-03", freq="D", tz="UTC")
    pd.DataFrame({"date": days, "close": 1.0}).to_feather(path)
    assert analyze.main(FakeRunner([strat("Good")]), TODAY) == 0
    late = pd.date_range("2024-12-31 00:05", periods=3, freq="5min", tz="UTC")
    pd.concat([pd.DataFrame({"date": days, "close": 1.0}), pd.DataFrame({"date": late, "close": 2.0})],
              ignore_index=True).to_feather(path)
    r = FakeRunner([strat("Good")])
    assert analyze.main(r, TODAY) == 0
    assert r.backtests == [("Good", "binanceus", dt.date(2024, 1, 1))]
    r = FakeRunner([strat("Good")])
    assert analyze.main(r, TODAY) == 0
    assert r.backtests == []


def test_failed_chunks_retry_three_times_and_fail_the_run(lab):
    found = [strat("Good"), strat("Flaky")]
    codes, calls = [], []
    for _ in range(4):
        r = FakeRunner(found, fail={"Flaky"})
        codes.append(analyze.main(r, TODAY))
        calls.append(len(r.backtests))
    assert calls == [50, 25, 25, 0] and codes == [1, 1, 1, 0]
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


def test_chunks_are_past_years_then_this_years_months_to_today():
    assert analyze.chunks(dt.date(2024, 6, 3), TODAY) == [
        (dt.date(2024, 1, 1), dt.date(2025, 1, 1)), (dt.date(2025, 1, 1), dt.date(2026, 1, 1))] + [
        (dt.date(2026, m, 1), dt.date(2026, m + 1, 1)) for m in range(1, 11)] + [(dt.date(2026, 11, 1), TODAY)]
    assert analyze.chunks(dt.date(2026, 1, 1), dt.date(2026, 1, 1)) == []
    assert analyze.chunks(dt.date(2026, 9, 20), dt.date(2026, 12, 31)) == [
        (dt.date(2026, 9, 1), dt.date(2026, 10, 1)), (dt.date(2026, 10, 1), dt.date(2026, 11, 1)),
        (dt.date(2026, 11, 1), dt.date(2026, 12, 1)), (dt.date(2026, 12, 1), dt.date(2026, 12, 31))]
    assert analyze.chunks(dt.date(2025, 3, 1), dt.date(2026, 1, 1)) == [(dt.date(2025, 1, 1), dt.date(2026, 1, 1))]


def test_a_past_years_chunk_digest_is_the_one_it_was_stored_with(lab):
    """The yearly digest the chunks used before months, so moving to months moved no stored key of a past year."""
    path = analyze.RESULTS / "data" / "binanceus" / "BTC_USD-5m.feather"
    path.parent.mkdir(parents=True)
    df = pd.DataFrame({"date": pd.date_range("2024-03-01", "2026-11-03", freq="h", tz="UTC"), "close": 1.0})
    df.to_feather(path)
    chunk = analyze.candle_digests("binanceus", "5m", "chunk", TODAY)
    for y in (2024, 2025):
        rows = df[df["date"].dt.year == y]
        old = hashlib.sha256(b"BTC/USD" + pd.util.hash_pandas_object(rows, index=False).values.tobytes()).hexdigest()
        assert chunk[dt.date(y, 1, 1)] == old
    assert sorted(k for k in chunk if k.year == 2026) == [dt.date(2026, m, 1) for m in range(1, 12)]


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
        def backtest(self, strategy, exchange, start, end, overlay=None):
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


def scored(conn, monkeypatch, *pairs) -> None:
    """Walk-forward has scored these strategy x exchange pairs, now and after each night's confidence rebuild."""
    rebuild = analyze.confidence.run

    def score() -> None:
        for s, e in pairs:
            conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) "
                         "VALUES (%s, %s, 60, false, 0, 40, '{}') ON CONFLICT (strategy, exchange) DO UPDATE SET score = 60",
                         (s, e))

    monkeypatch.setattr(analyze.confidence, "run", lambda conn: (rebuild(conn), score())[0])
    score()


EVERY = [(s, e) for s in ("Good", "Other") for e in ("binanceus", "coinbase")]

TOY = """
[sweep]
max_chunks = {max_chunks}

[overlays.slots]
max_open_trades = [1, 5]

[overlays.majors]
pairs = [3]
"""


def test_overlays_run_as_counted_trials_and_cache_like_any_chunk(lab, tmp_path, monkeypatch):
    lab.execute("DELETE FROM trials WHERE variant LIKE 'overlay:%%'")  # the fixture keeps trials' seeded rows
    scored(lab, monkeypatch, *EVERY)
    found = [strat("Good"), strat("Other")]
    assert analyze.main(FakeRunner(found), TODAY) == 0
    base_keys = lab.execute("SELECT id, cache_key FROM backtest_chunks ORDER BY id").fetchall()
    trials = one(lab, "SELECT count(*) FROM trials")
    dsr = one(lab, "SELECT trials_raw FROM strategy_windows WHERE win = '365d' LIMIT 1")

    (tmp_path / "variants.toml").write_text(TOY.format(max_chunks=1000))
    r = FakeRunner(found)
    assert analyze.main(r, TODAY) == 0
    # 3 overlays x 2 strategies x 25 chunks (binanceus 2024, 2025, coinbase 2025, 11 months each); no as-is chunk reruns
    assert len(r.backtests) == 150 and all(len(b) == 4 for b in r.backtests)
    assert lab.execute("SELECT id, cache_key FROM backtest_chunks WHERE overlay = '' ORDER BY id").fetchall() == base_keys
    assert {b[3].get("exchange", {}).get("pair_whitelist", [None])[-1] for b in r.backtests if "exchange" in b[3]} == \
        {analyze.pair_list("binanceus")[2], analyze.pair_list("coinbase")[2]}
    # the register grows by exactly the strategy x venue x overlay series, and their returns raise the bar
    assert one(lab, "SELECT count(*) FROM trials") - trials == 12
    assert one(lab, "SELECT count(*) FROM trials WHERE variant LIKE 'overlay:%%'") == 12
    assert one(lab, "SELECT count(DISTINCT (strategy, exchange, overlay)) FROM overlay_windows") == 12
    assert one(lab, "SELECT trials_raw FROM strategy_windows WHERE win = '365d' LIMIT 1") == dsr * 4
    assert one(lab, "SELECT count(*) FROM overlays") == 3
    assert one(lab, "SELECT raw->'overlay_chunks_run' FROM runs WHERE kind = 'analysis' ORDER BY id DESC LIMIT 1") == 150

    r2 = FakeRunner(found)
    assert analyze.main(r2, TODAY) == 0
    assert r2.backtests == [] and one(lab, "SELECT count(*) FROM trials") - trials == 12

    # an overlay taken out of the toml takes its results; its trials stay counted
    (tmp_path / "variants.toml").write_text("[overlays.slots]\nmax_open_trades = [1]\n")
    assert analyze.main(FakeRunner(found), TODAY) == 0
    assert one(lab, "SELECT count(DISTINCT overlay) FROM backtest_chunks WHERE overlay <> ''") == 1
    assert one(lab, "SELECT count(*) FROM trials") - trials == 12


def test_overlay_chunks_are_capped_a_night_best_prior_score_first(lab, tmp_path, monkeypatch):
    scored(lab, monkeypatch, *EVERY)
    found = [strat("Good"), strat("Other")]
    assert analyze.main(FakeRunner(found), TODAY) == 0
    best = lab.execute("SELECT strategy, exchange FROM strategy_windows WHERE win = '365d' "
                       "ORDER BY sharpe_lo DESC LIMIT 1").fetchone()
    (tmp_path / "variants.toml").write_text(TOY.format(max_chunks=2))
    r = FakeRunner(found)
    assert analyze.main(r, TODAY) == 0
    assert len(r.backtests) == 2 and {b[:2] for b in r.backtests} == {tuple(best)}
    # the series' newest chunk first
    assert dt.date(2026, 11, 1) in {r.backtests[0][2], r.backtests[1][2]}
    raw = one(lab, "SELECT raw FROM runs WHERE kind = 'analysis' ORDER BY id DESC LIMIT 1")
    assert raw["overlay_chunks_run"] == 2 and raw["overlay_chunks_waiting"] == 148


def test_overlays_run_only_on_series_walk_forward_scored(lab, tmp_path, monkeypatch):
    scored(lab, monkeypatch, ("Good", "binanceus"))
    (tmp_path / "variants.toml").write_text(TOY.format(max_chunks=1000))
    r = FakeRunner([strat("Good"), strat("Other")])
    assert analyze.main(r, TODAY) == 0
    # 3 overlays x 13 binanceus chunks of the one scored series; the as-is chunks of all four still run
    over = [b for b in r.backtests if len(b) == 4]
    assert len(over) == 39 and {b[:2] for b in over} == {("Good", "binanceus")}
    assert len(r.backtests) - len(over) == 50
    assert one(lab, "SELECT raw->'overlay_chunks_waiting' FROM runs ORDER BY id DESC LIMIT 1") == 0


def test_refreshing_the_current_year_rotates_through_every_overlay_series(lab, tmp_path, monkeypatch):
    scored(lab, monkeypatch, *EVERY)
    found = [strat("Good"), strat("Other")]
    (tmp_path / "variants.toml").write_text(TOY.format(max_chunks=1000))
    assert analyze.main(FakeRunner(found), TODAY) == 0
    (tmp_path / "variants.toml").write_text(TOY.format(max_chunks=4))
    nights = []
    for day in (1, 2, 3):
        r = FakeRunner(found)
        assert analyze.main(r, TODAY + dt.timedelta(days=day)) == 0
        nights.append({b[:2] + (json.dumps(b[3], sort_keys=True),) for b in r.backtests if len(b) == 4})
    # 12 overlay series each have a stale current month; 4 a night, none twice in three nights
    assert all(len(n) == 4 for n in nights) and len(nights[0] | nights[1] | nights[2]) == 12


def test_detail_requires_complete_candles_for_every_whitelisted_pair(tmp_path, monkeypatch):
    from freqtrade.misc import pair_to_filename
    monkeypatch.setattr(analyze, 'RESULTS', tmp_path)
    monkeypatch.setattr(analyze, 'whitelist', lambda ex: {'BTC/USD', 'ETH/USD'})
    data = tmp_path / 'data' / 'venue'
    data.mkdir(parents=True)
    start, end = dt.date(2023, 1, 1), dt.date(2023, 1, 2)
    dates = pd.date_range(str(start), str(end), freq='1min', inclusive='left', tz='UTC')
    for pair in ('BTC/USD', 'ETH/USD'):
        pd.DataFrame({'date': dates}).to_feather(data / f'{pair_to_filename(pair)}-1m.feather')
    assert analyze.detail_coverage('venue', '1m', start, end) is None
    pd.DataFrame({'date': dates.delete(500)}).to_feather(data / 'ETH_USD-1m.feather')
    assert 'incomplete' in analyze.detail_coverage('venue', '1m', start, end)
    (data / 'ETH_USD-1m.feather').unlink()
    assert 'missing' in analyze.detail_coverage('venue', '1m', start, end)
