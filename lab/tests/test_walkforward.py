import datetime as dt
import json
import types

import pytest

from lab import analyze, metrics
from lab import walkforward as wf
from test_analyze import REPO

TODAY = dt.date(2026, 11, 4)
TUNE = {"spaces": "default", "epochs": 150, "early_stop": 40, "sampler": "NSGAIIISampler"}


class FakeRunner:
    """A fold's result from its strategy and month; `fail`, `hole` and `untuned` name strategies."""

    def __init__(self, fail=frozenset(), hole=frozenset(), untuned=frozenset()):
        self.fail, self.hole, self.untuned = set(fail), set(hole), set(untuned)
        self.calls: list[tuple] = []

    def version(self):
        return "test"

    def fold(self, strategy, file, exchange, fold, seed):
        self.calls.append((strategy, fold[1], seed, exchange))
        if strategy in self.fail:
            return {"ok": False, "error": "hyperopt exit 2: boom", "duration_s": 1.0}
        if strategy in self.hole:
            return {"ok": False, "no_data": True, "error": f"base: {analyze.NO_DATA}", "duration_s": 1.0}
        def side(p, n):
            d = dt.datetime(fold[1].year, fold[1].month, 1, tzinfo=dt.timezone.utc)
            dated = [("BTC/USDT", d + dt.timedelta(days=i), d + dt.timedelta(days=i, hours=2), p / n) for i in range(n)]
            return {"trades": n, "profit": p, "expectancy": p / n, "returns": [p / n] * n, "dated": dated}
        out = {"ok": True, "error": None, "duration_s": 2.0, "hyperopt_s": 1.5, "backtest_s": 0.5, "epochs_run": 70,
               "base": side(0.01, 10), "params": None}
        if strategy in self.untuned:
            return {**out, "error": "no hyperopt epoch reached 10 training trades"}
        return {**out, "params": {"roi": {"0": 0.03}, "stoploss": -0.08}, "tuned": side(0.04, 12), "is": side(0.2, 70)}


@pytest.fixture
def wlab(conn, monkeypatch):
    monkeypatch.setattr(analyze, "USER_DATA", REPO / "user_data")
    monkeypatch.setattr(wf, "ONLY", set())
    monkeypatch.setattr(wf, "FOLDS", 3)
    monkeypatch.setattr(wf, "WORKERS", 2)
    monkeypatch.setattr(wf, "EXCHANGES", ["binanceus"])
    box = dict(wf.suggest.load_config(), lockbox={"start": dt.date(2099, 1, 1)})
    monkeypatch.setattr(wf.suggest, "load_config", lambda: box)
    monkeypatch.setattr(wf.suggest, "run", lambda c: {})  # keeps the fixture's suggestions, which pick the candidates
    backfilled(conn, "binanceus", "2019-09-01")
    now = dt.datetime.now(dt.timezone.utc)
    for name in ("Live", "Promising", "Watched", "Leaky", "Broken", "Idle"):
        conn.execute("INSERT INTO strategies (name, file, file_hash, timeframe, load_error) VALUES (%s, %s, %s, '5m', %s)",
                     (name, f"user_data/strategies/{name}.py", f"{name}-1", "boom" if name == "Broken" else None))
    conn.execute("INSERT INTO bots (name, exchange, strategy, last_seen) VALUES ('binanceus-live', 'binanceus', 'Live', %s), "
                 "('kraken-idle', 'kraken', 'Idle', %s)", (now, now - dt.timedelta(days=3)))
    for kind, name, score in (("watch", "Watched", 0.9), ("promote", "Promising", 0.1), ("watch", "Leaky", 5.0),
                              ("watch", "Broken", 5.0)):
        conn.execute("INSERT INTO suggestions (kind, strategy, exchange, bot, score, summary, action, gates) "
                     "VALUES (%s, %s, 'binanceus', '', %s, 's', 'a', '[]')", (kind, name, score))
    conn.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias) VALUES ('Leaky', 'lookahead', 'Leaky-1', true)")
    return conn


def test_folds_stop_at_the_lockbox():
    got = wf.folds(dt.date(2026, 11, 5), n=2, train_days=180, lockbox=dt.date(2026, 5, 1))
    assert [(f[1], f[2]) for f in got] == [(dt.date(2026, 3, 1), dt.date(2026, 4, 1)), (dt.date(2026, 4, 1), dt.date(2026, 5, 1))]


def test_a_lockbox_in_the_future_changes_nothing():
    today = dt.date(2026, 3, 5)
    assert wf.folds(today, n=2, train_days=180, lockbox=dt.date(2026, 5, 1)) == wf.folds(today, n=2, train_days=180)


def backfilled(conn, ex, first):
    for pair in analyze.whitelist(ex):
        conn.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
                     "VALUES (%s, %s, '5m', %s, now(), 1, true)", (ex, pair, first))


def test_folds_are_the_last_whole_months_oldest_first():
    assert wf.folds(TODAY, 3, 180) == [
        (dt.date(2026, 2, 2), dt.date(2026, 8, 1), dt.date(2026, 9, 1)),
        (dt.date(2026, 3, 5), dt.date(2026, 9, 1), dt.date(2026, 10, 1)),
        (dt.date(2026, 4, 4), dt.date(2026, 10, 1), dt.date(2026, 11, 1))]
    assert wf.folds(dt.date(2026, 1, 31), 1, 30)[0] == (dt.date(2025, 11, 1), dt.date(2025, 12, 1), dt.date(2026, 1, 1))


def test_candidates_are_the_league_then_promote_then_watch_and_skip_biased_or_broken(wlab):
    assert [c["name"] for c in wf.candidates(wlab)] == ["Live", "Promising", "Watched"]


def test_then_each_venues_best_year_in_turn_up_to_the_limit(wlab, monkeypatch):
    monkeypatch.setattr(wf, "EXCHANGES", ["binanceus", "okx"])
    for name in ("Okx1", "Okx2", "Bus1", "Coin1"):
        wlab.execute("INSERT INTO strategies (name, file, file_hash, timeframe) VALUES (%s, 'f.py', 'h', '5m')", (name,))
    for name, ex, lo in (("Okx1", "okx", 0.003), ("Okx2", "okx", 0.002), ("Bus1", "binanceus", 0.001),
                         ("Leaky", "okx", 0.009), ("Coin1", "coinbase", 0.05), ("Live", "binanceus", 0.004)):
        wlab.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades, expectancy_lo) "
                     "VALUES (%s, %s, '365d', 365, 100, %s)", (name, ex, lo))
    assert [c["name"] for c in wf.candidates(wlab)] == ["Live", "Promising", "Watched", "Okx1", "Bus1", "Okx2"]
    monkeypatch.setattr(wf, "LIMIT", 4)
    assert [c["name"] for c in wf.candidates(wlab)] == ["Live", "Promising", "Watched", "Okx1"]


def test_every_backfilled_venue_gets_its_own_folds(wlab, monkeypatch):
    monkeypatch.setattr(wf, "EXCHANGES", ["binanceus", "okx", "kucoin"])
    monkeypatch.setattr(wf, "ONLY", {"Live"})
    backfilled(wlab, "okx", "2026-03-01")  # the oldest fold trains from 2026-02-02, so it is skipped there
    runner = FakeRunner()
    assert wf.main(runner, TODAY) == 0
    assert sorted((c[3], c[1]) for c in runner.calls) == [
        ("binanceus", dt.date(2026, 8, 1)), ("binanceus", dt.date(2026, 9, 1)), ("binanceus", dt.date(2026, 10, 1)),
        ("okx", dt.date(2026, 9, 1)), ("okx", dt.date(2026, 10, 1))]
    assert all(seed == metrics.seed_of(s, ex, start) for s, start, seed, ex in runner.calls)
    assert dict(wlab.execute("SELECT exchange, count(*) FROM walkforward GROUP BY 1").fetchall()) == {"binanceus": 3, "okx": 2}
    raw = wlab.execute("SELECT raw FROM runs WHERE kind = 'walkforward'").fetchone()[0]
    assert raw["exchanges"] == ["binanceus", "okx", "kucoin"]
    assert sorted(raw["universe"]) == ["binanceus", "kucoin", "okx"] and raw["universe"]["okx"]["source"] == "current-listing"
    assert "Live on kucoin: 5m not fully backfilled" in raw["skipped"]
    assert "Live on okx 2026-08: training starts before its first candle" in raw["skipped"]


def test_a_run_stores_each_fold_and_the_next_run_reuses_it(wlab):
    runner = FakeRunner()
    assert wf.main(runner, TODAY) == 0
    assert len(runner.calls) == 9
    assert sorted({c[1] for c in runner.calls}) == [dt.date(2026, 8, 1), dt.date(2026, 9, 1), dt.date(2026, 10, 1)]
    assert runner.calls[0][1] == dt.date(2026, 10, 1), "the newest month runs first"
    assert all(seed == metrics.seed_of(s, "binanceus", start) for s, start, seed, _ in runner.calls)
    row = wlab.execute("SELECT ok, params, base_trades, tuned_profit, is_trades, array_length(tuned_returns, 1), train_start "
                       "FROM walkforward WHERE strategy = 'Live' AND fold_start = '2026-10-01'").fetchone()
    assert row == (True, {"roi": {"0": 0.03}, "stoploss": -0.08}, 10, 0.04, 70, 12, dt.date(2026, 4, 4))
    raw = wlab.execute("SELECT raw, ok FROM runs WHERE kind = 'walkforward'").fetchone()
    assert raw[1] and raw[0]["folds_run"] == 9 and raw[0]["candidates"] == ["Live", "Promising", "Watched"]
    again = FakeRunner()
    assert wf.main(again, TODAY) == 0 and again.calls == []
    assert wlab.execute("SELECT raw->>'folds_cached' FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "9"


def test_a_new_month_or_a_changed_file_reruns_only_what_it_touches(wlab):
    wf.main(FakeRunner(), TODAY)
    later = FakeRunner()
    wf.main(later, dt.date(2026, 12, 2))
    assert sorted(c[0] for c in later.calls) == ["Live", "Promising", "Watched"]
    assert {c[1] for c in later.calls} == {dt.date(2026, 11, 1)}
    wlab.execute("UPDATE strategies SET file_hash = 'Live-2' WHERE name = 'Live'")
    edited = FakeRunner()
    wf.main(edited, dt.date(2026, 12, 2))
    assert sorted(c[1] for c in edited.calls) == [dt.date(2026, 9, 1), dt.date(2026, 10, 1), dt.date(2026, 11, 1)]
    assert {c[0] for c in edited.calls} == {"Live"}


def test_a_run_tests_no_month_inside_the_lockbox(wlab, monkeypatch):
    box = dict(wf.suggest.load_config(), lockbox={"start": dt.date(2026, 9, 1)})
    monkeypatch.setattr(wf.suggest, "load_config", lambda: box)
    runner = FakeRunner()
    wf.main(runner, TODAY)
    assert runner.calls and max(c[1] for c in runner.calls) == dt.date(2026, 8, 1)


def test_failures_retry_up_to_the_cap_and_a_month_with_no_candles_is_stored_once(wlab, monkeypatch):
    monkeypatch.setattr(wf, "ONLY", {"Live", "Watched"})
    assert wf.main(FakeRunner(fail={"Live"}, hole={"Watched"}), TODAY) == 1
    rows = dict(wlab.execute("SELECT strategy, max(attempts) FROM walkforward GROUP BY 1").fetchall())
    assert rows == {"Live": 1, "Watched": analyze.MAX_ATTEMPTS}
    for _ in range(analyze.MAX_ATTEMPTS):
        wf.main(FakeRunner(fail={"Live"}, hole={"Watched"}), TODAY)
    last = FakeRunner(fail={"Live"}, hole={"Watched"})
    assert wf.main(last, TODAY) == 0 and last.calls == []
    errors = {e for (e,) in wlab.execute("SELECT error FROM walkforward")}
    assert errors == {"hyperopt exit 2: boom", f"base: {analyze.NO_DATA}"}


def test_a_fold_hyperopt_could_not_tune_is_ok_and_keeps_its_base(wlab, monkeypatch):
    monkeypatch.setattr(wf, "ONLY", {"Live"})
    assert wf.main(FakeRunner(untuned={"Live"}), TODAY) == 0
    ok, params, base, tuned, err = wlab.execute(
        "SELECT ok, params, base_profit, tuned_profit, error FROM walkforward LIMIT 1").fetchone()
    assert ok and params is None and base == 0.01 and tuned is None and "no hyperopt epoch" in err


def test_retune_rows_come_from_stored_folds(wlab, monkeypatch):
    from lab import suggest

    monkeypatch.setattr(wf, "ONLY", {"Live"})
    wf.main(FakeRunner(), TODAY)
    box = suggest.load_config()
    monkeypatch.undo()
    monkeypatch.setattr(suggest, "load_config", lambda: box)
    assert "retune" not in suggest.run(wlab)["suggestions"], "no lookahead verdict yet, so no retune"
    wlab.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias) VALUES ('Live', 'lookahead', 'Live-1', false)")
    assert suggest.run(wlab)["suggestions"]["retune"] == 1
    summary, action = wlab.execute("SELECT summary, action FROM suggestions WHERE kind = 'retune'").fetchone()
    assert "in each of the newest 2 unseen months" in summary
    params = json.loads(action.split(":\n", 1)[1])
    assert params == {"strategy_name": "Live", "params": {"roi": {"0": 0.03}, "stoploss": -0.08}, "ft_stratparam_v": 1}


def test_stats_purge_trades_force_closed_at_the_span_end():
    import pandas as pd
    t = pd.DataFrame({"profit_ratio": [0.01, -0.5, 0.02], "profit_abs": [1.0, -50.0, 2.0],
                      "exit_reason": ["roi", "force_exit", "roi"]})
    got = wf.stats({"trades": t, "start_balance": 100.0, "final_balance": 53.0})
    assert got["trades"] == 2 and got["returns"] == [0.01, 0.02]
    assert got["profit"] == pytest.approx(0.03)  # the straddling trade's -50% is gone


def test_training_stops_an_embargo_before_the_test_month(monkeypatch):
    monkeypatch.setattr(wf, "EMBARGO_DAYS", 7)
    assert wf.train_end(dt.date(2026, 9, 1)) == dt.date(2026, 8, 25)


def test_the_embargo_is_part_of_the_cache_key(monkeypatch):
    fold = wf.folds(TODAY, 1, 180)[0]
    monkeypatch.setattr(analyze, "config_files", lambda ex: [])
    monkeypatch.setattr(wf, "EMBARGO_DAYS", 7)
    a = wf.fold_key("h", "binanceus", fold, "v", "candles", TUNE)
    monkeypatch.setattr(wf, "EMBARGO_DAYS", 3)
    assert a != wf.fold_key("h", "binanceus", fold, "v", "candles", TUNE)


def test_a_changed_candle_reruns_only_the_folds_whose_span_holds_it(wlab, monkeypatch, tmp_path):
    from test_families import synthetic_candles

    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    feather = tmp_path / "data" / "binanceus" / "BTC_USD-5m.feather"
    feather.parent.mkdir(parents=True)
    utc = dt.timezone.utc
    synthetic_candles(dt.datetime(2026, 1, 1, tzinfo=utc), dt.datetime(2026, 11, 3, tzinfo=utc), "5min").to_feather(feather)
    wf.main(FakeRunner(), TODAY)

    def touch(day):
        df = wf.analyze.pd.read_feather(feather)
        df.loc[df["date"] == wf.analyze.pd.Timestamp(day, tz="UTC"), "volume"] += 1
        df.to_feather(feather)
        runner = FakeRunner()
        wf.main(runner, TODAY)
        return sorted({(s, m) for s, m, _, _ in runner.calls})

    assert touch("2026-01-15") == []  # before every fold's training span
    # 2026-02-10 is only in the oldest fold's training span (Feb 2 to Aug 1); the others train from March
    assert touch("2026-02-10") == [(s, dt.date(2026, 8, 1)) for s in ("Live", "Promising", "Watched")]


def test_dated_test_month_trades_are_stored_per_variant_and_replaced_on_rerun(wlab):
    wf.main(FakeRunner(), TODAY)
    q = ("SELECT variant, count(*), sum(profit_ratio) FROM walkforward_trades "
         "WHERE strategy = 'Live' AND fold_start = '2026-10-01' GROUP BY variant ORDER BY variant")
    got = wlab.execute(q).fetchall()
    assert [(v, n) for v, n, _ in got] == [("base", 10), ("har_target", 12), ("tuned", 12)]
    assert got[1][2] == 0, "without forecasts the HAR sleeve holds cash"
    assert got[2][2] == pytest.approx(0.04)
    wlab.execute("UPDATE walkforward SET cache_key = 'stale' WHERE strategy = 'Live'")
    wf.main(FakeRunner(), TODAY)
    assert wlab.execute(q).fetchall() == got, "a rerun replaces every variant, never doubles its trades"
    day = wlab.execute("SELECT trades FROM walkforward_daily WHERE strategy = 'Live' AND variant = 'tuned' "
                       "AND day = '2026-10-01'").fetchone()
    assert day == (1,)


def test_stats_keep_the_dates_of_the_trades_that_survive_the_purge():
    import pandas as pd
    o = pd.to_datetime(["2026-10-02", "2026-10-30"], utc=True)
    t = pd.DataFrame({"pair": ["A/USDT", "B/USDT"], "open_date": o, "close_date": o + pd.Timedelta(hours=3),
                      "profit_ratio": [0.01, -0.5], "profit_abs": [1.0, -50.0], "exit_reason": ["roi", "force_exit"]})
    got = wf.stats({"trades": t, "start_balance": 100.0})
    assert [d[0] for d in got["dated"]] == ["A/USDT"] and got["dated"][0][3] == 0.01


def test_a_spent_budget_starts_no_more_folds_and_still_records_the_run(wlab, monkeypatch):
    monkeypatch.setattr(wf, "WORKERS", 1)
    monkeypatch.setattr(wf, "BUDGET_S", 60.0)
    clock = iter([0.0, 1.0])  # the run starts at 0, the first fold at 1; every later fold starts past the budget
    monkeypatch.setattr(wf, "time", types.SimpleNamespace(monotonic=lambda: next(clock, 61.0)))
    runner = FakeRunner()
    assert wf.main(runner, TODAY) == 0
    assert len(runner.calls) == 1 and wlab.execute("SELECT count(*) FROM walkforward").fetchone()[0] == 1
    raw, ok = wlab.execute("SELECT raw, ok FROM runs WHERE kind = 'walkforward'").fetchone()
    assert ok and raw["folds_run"] == 9 and raw["folds_left"] == 8 and raw["folds_failed"] == 0
    monkeypatch.setattr(wf, "BUDGET_S", 0.0)
    rest = FakeRunner()
    assert wf.main(rest, TODAY) == 0 and len(rest.calls) == 8, "the next run carries on where this one stopped"


def test_robustness_configuration_invalidates_cached_folds(monkeypatch):
    cfg = wf.suggest.load_config()
    first = wf.fold_key('h', 'binanceus', (TODAY, TODAY, TODAY), 'v', 'candles', TUNE)
    monkeypatch.setattr(wf.suggest, 'load_config', lambda: {**cfg, 'confidence': {
        **cfg['confidence'], 'entry_delay_candles': cfg['confidence']['entry_delay_candles'] + 1}})
    assert wf.fold_key('h', 'binanceus', (TODAY, TODAY, TODAY), 'v', 'candles', TUNE) != first


def test_detail_candles_invalidate_fold_cache(wlab, monkeypatch):
    calls = []
    def digest(exchange, timeframe, by='year'):
        calls.append(timeframe)
        return {dt.date(2026, 10, 1): digest.value if timeframe == '1m' else 'base'}
    digest.value = 'detail-1'
    monkeypatch.setattr(analyze, 'candle_digests', digest)
    wf.main(FakeRunner(), TODAY)
    digest.value = 'detail-2'
    runner = FakeRunner()
    wf.main(runner, TODAY)
    assert len(runner.calls) == 3  # Only the newest test month includes the changed October candle.


def test_a_strategy_without_parameters_tunes_roi_and_stoploss_with_fewer_epochs(monkeypatch):
    monkeypatch.setattr(analyze, "REPO", REPO)
    monkeypatch.setattr(wf, "SAMPLERS", ["NSGAIIISampler"])
    assert wf.tuning("user_data/strategies/Diamond.py", 1) == {
        "spaces": "default", "epochs": wf.EPOCHS, "early_stop": wf.EARLY_STOP, "sampler": "NSGAIIISampler"}
    assert wf.tuning("user_data/strategies/BreakEven.py", 1)["spaces"] == "roi stoploss"
    assert wf.tuning("user_data/strategies/BreakEven.py", 1)["epochs"] == wf.EPOCHS_ROI
    assert wf.has_parameters("user_data/strategies/Missing.py"), "an unreadable file gets the full search"


def test_parameters_in_an_imported_sibling_count(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "REPO", tmp_path)
    (tmp_path / "Base.py").write_text("from freqtrade.strategy import IntParameter\nx = IntParameter(1, 5, default=2)\n")
    (tmp_path / "Child.py").write_text("from Base import Base\n")
    (tmp_path / "Alone.py").write_text("from freqtrade.strategy import IStrategy\n")
    assert wf.has_parameters("Child.py") and not wf.has_parameters("Alone.py")


def test_every_fold_draws_a_sampler_arm_from_its_seed(monkeypatch):
    monkeypatch.setattr(wf, "SAMPLERS", ["NSGAIIISampler", "TPESampler"])
    arms = {wf.tuning("missing.py", metrics.seed_of("S", "binanceus", m))["sampler"] for m in range(1, 13)}
    assert arms == {"NSGAIIISampler", "TPESampler"}


def test_the_tuning_settings_are_part_of_the_cache_key(monkeypatch):
    monkeypatch.setattr(analyze, "config_files", lambda ex: [])
    fold = wf.folds(TODAY, 1, 180)[0]
    keys = {wf.fold_key("h", "binanceus", fold, "v", "candles", {**TUNE, k: v})
            for k, v in (("spaces", "roi stoploss"), ("epochs", 60), ("early_stop", 0), ("sampler", "TPESampler"))}
    assert len(keys | {wf.fold_key("h", "binanceus", fold, "v", "candles", TUNE)}) == 5


def test_the_sampler_override_sets_only_generate_estimator():
    class Plain:
        pass

    class Custom:
        class HyperOpt:
            @staticmethod
            def roi_space():
                return ["kept"]

    for name, cls in (("Plain", Plain), ("Custom", Custom)):
        exec(wf.sampler_override(name, "TPESampler"), {name: cls})
        assert cls.HyperOpt.generate_estimator(dimensions=[], random_state=1) == "TPESampler"
    assert Custom.HyperOpt.roi_space() == ["kept"]


def test_folds_store_their_step_seconds_and_the_arms_report_compares_them(wlab, monkeypatch):
    monkeypatch.setattr(wf, "SAMPLERS", ["NSGAIIISampler", "TPESampler"])
    assert wf.main(FakeRunner(), TODAY) == 0
    rows = wlab.execute("SELECT hyperopt_s, backtest_s, epochs_run, spaces, epochs, sampler FROM walkforward").fetchall()
    assert len(rows) == 9 and {r[:5] for r in rows} == {(1.5, 0.5, 70, "default", 150)}
    raw = wlab.execute("SELECT raw FROM runs WHERE kind = 'walkforward'").fetchone()[0]
    arms = {a["sampler"]: a for a in raw["arms"]}
    assert sum(a["folds"] for a in arms.values()) == 9 and set(arms) == {r[5] for r in rows}
    assert all(a["hyperopt_s"] == 1.5 and a["oos_uplift"] == pytest.approx(0.04 / 12 - 0.001) for a in arms.values())
