import datetime as dt
import json

import pytest

import analyze
import metrics
import walkforward as wf
from test_analyze import REPO

TODAY = dt.date(2026, 11, 4)


class FakeRunner:
    """A fold's result from its strategy and month; `fail`, `hole` and `untuned` name strategies."""

    def __init__(self, fail=frozenset(), hole=frozenset(), untuned=frozenset()):
        self.fail, self.hole, self.untuned = set(fail), set(hole), set(untuned)
        self.calls: list[tuple] = []

    def version(self):
        return "test"

    def fold(self, strategy, file, exchange, fold, seed):
        self.calls.append((strategy, fold[1], seed))
        if strategy in self.fail:
            return {"ok": False, "error": "hyperopt exit 2: boom", "duration_s": 1.0}
        if strategy in self.hole:
            return {"ok": False, "no_data": True, "error": f"base: {analyze.NO_DATA}", "duration_s": 1.0}
        side = lambda p, n: {"trades": n, "profit": p, "expectancy": p / n, "returns": [p / n] * n}  # noqa: E731
        out = {"ok": True, "error": None, "duration_s": 2.0, "base": side(0.01, 10), "params": None}
        if strategy in self.untuned:
            return {**out, "error": "no hyperopt epoch reached 10 training trades"}
        return {**out, "params": {"roi": {"0": 0.03}, "stoploss": -0.08}, "tuned": side(0.04, 12), "is": side(0.2, 70)}


@pytest.fixture
def wlab(conn, monkeypatch):
    monkeypatch.setattr(analyze, "USER_DATA", REPO / "user_data")
    monkeypatch.setattr(wf, "ONLY", set())
    monkeypatch.setattr(wf, "FOLDS", 3)
    monkeypatch.setattr(wf, "WORKERS", 2)
    monkeypatch.setattr(wf.suggest, "run", lambda c: {})  # keeps the fixture's suggestions, which pick the candidates
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


def test_folds_are_the_last_whole_months_oldest_first():
    assert wf.folds(TODAY, 3, 180) == [
        (dt.date(2026, 2, 2), dt.date(2026, 8, 1), dt.date(2026, 9, 1)),
        (dt.date(2026, 3, 5), dt.date(2026, 9, 1), dt.date(2026, 10, 1)),
        (dt.date(2026, 4, 4), dt.date(2026, 10, 1), dt.date(2026, 11, 1))]
    assert wf.folds(dt.date(2026, 1, 31), 1, 30)[0] == (dt.date(2025, 11, 1), dt.date(2025, 12, 1), dt.date(2026, 1, 1))


def test_candidates_are_the_league_then_promote_then_watch_and_skip_biased_or_broken(wlab):
    assert [c["name"] for c in wf.candidates(wlab)] == ["Live", "Promising", "Watched"]


def test_a_run_stores_each_fold_and_the_next_run_reuses_it(wlab):
    runner = FakeRunner()
    assert wf.main(runner, TODAY) == 0
    assert len(runner.calls) == 9
    assert sorted({c[1] for c in runner.calls}) == [dt.date(2026, 8, 1), dt.date(2026, 9, 1), dt.date(2026, 10, 1)]
    assert runner.calls[0][1] == dt.date(2026, 10, 1), "the newest month runs first"
    assert all(seed == metrics.seed_of(s, "binanceus", start) for s, start, seed in runner.calls)
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
    import suggest

    monkeypatch.setattr(wf, "ONLY", {"Live"})
    wf.main(FakeRunner(), TODAY)
    monkeypatch.undo()
    assert "retune" not in suggest.run(wlab)["suggestions"], "no lookahead verdict yet, so no retune"
    wlab.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias) VALUES ('Live', 'lookahead', 'Live-1', false)")
    assert suggest.run(wlab)["suggestions"]["retune"] == 1
    summary, action = wlab.execute("SELECT summary, action FROM suggestions WHERE kind = 'retune'").fetchone()
    assert "tuned beat as-is on the newest 2 unseen months" in summary
    params = json.loads(action.split(":\n", 1)[1])
    assert params == {"strategy_name": "Live", "params": {"roi": {"0": 0.03}, "stoploss": -0.08}, "ft_stratparam_v": 1}
