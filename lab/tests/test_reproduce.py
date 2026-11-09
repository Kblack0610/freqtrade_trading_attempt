"""lab/reproduce.py: a sample of cached chunks and folds run again and compared with what is stored."""

import random

import pytest

from lab import analyze, reproduce
from lab import walkforward as wf
from test_analyze import TODAY, FakeRunner, lab, strat  # noqa: F401 - lab is a fixture
from test_walkforward import TODAY as WF_TODAY
from test_walkforward import FakeRunner as FakeFolds
from test_walkforward import wlab  # noqa: F401 - a fixture


def last_run(conn) -> tuple[bool, dict]:
    return conn.execute("SELECT ok, raw FROM runs WHERE kind = 'reproduce' ORDER BY id DESC LIMIT 1").fetchone()


class Drifted(FakeRunner):
    """The same backtests with one trade's profit nudged: what a nondeterministic run would hand back."""

    def backtest(self, strategy, exchange, start, end):
        res = super().backtest(strategy, exchange, start, end)
        if res["ok"] and len(res["trades"]):
            res["trades"].loc[0, "profit_ratio"] += 1e-12
        return res


class Retuned(FakeFolds):
    def fold(self, strategy, file, exchange, fold, seed):
        res = super().fold(strategy, file, exchange, fold, seed)
        return {**res, "params": {**res["params"], "stoploss": -0.09}}


@pytest.fixture
def chunks(lab, monkeypatch):
    found = [strat("Good"), strat("Other")]
    assert analyze.main(FakeRunner(found), TODAY) == 0
    monkeypatch.setattr(reproduce, "CHUNKS", 3)
    monkeypatch.setattr(reproduce, "FOLDS", 0)
    return lab, found


def test_cached_chunks_run_again_identical(chunks):
    conn, found = chunks
    assert reproduce.main(FakeRunner(found), FakeFolds(), random.Random(1)) == 0
    ok, raw = last_run(conn)
    assert ok and (raw["checked"], raw["mismatch"], raw["failed"]) == (3, 0, 0)
    assert [r["diff"] for r in raw["results"]] == [[], [], []]
    assert conn.execute("SELECT count(*) FROM backtest_chunks WHERE ok AND (revision, length(config_digest)) "
                        "IS DISTINCT FROM (%s, 128)", (analyze.REVISION,)).fetchone()[0] == 0


def test_a_chunk_that_comes_out_different_is_a_mismatch(chunks):
    conn, found = chunks
    assert reproduce.main(Drifted(found), FakeFolds(), random.Random(1)) == 0  # it ran: the mismatch has its own alert
    ok, raw = last_run(conn)
    assert ok and raw["checked"] == 3 and raw["mismatch"] == 3
    assert all("first difference at trade" in r["diff"][0] for r in raw["results"])


def test_only_chunks_whose_inputs_still_give_their_key_are_sampled(chunks):
    conn, found = chunks
    conn.execute("UPDATE strategies SET file_hash = file_hash || '-edited'")  # tonight's analysis would rerun them
    assert reproduce.chunk_sample(conn, random.Random(1), 10, "test") == []
    assert reproduce.chunk_sample(conn, random.Random(1), 10, "other freqtrade") == []


def test_a_failed_rerun_fails_the_run_without_counting_as_checked(chunks):
    conn, found = chunks
    assert reproduce.main(FakeRunner(found, fail={"Good", "Other"}), FakeFolds(), random.Random(1)) == 1
    ok, raw = last_run(conn)
    assert not ok and (raw["checked"], raw["failed"]) == (0, 3)


@pytest.fixture
def folds(wlab, monkeypatch):
    assert wf.main(FakeFolds(), WF_TODAY) == 0
    monkeypatch.setattr(reproduce, "CHUNKS", 0)
    monkeypatch.setattr(reproduce, "FOLDS", 2)
    return wlab


def test_folds_store_their_provenance(folds):
    rows = folds.execute("SELECT strategy, exchange, fold_start, revision, config_digest, candles, seed FROM walkforward").fetchall()
    assert rows and all(r[3] == analyze.REVISION and len(r[4]) == 128 and len(r[5]) == 64 for r in rows)
    assert all(r[6] == wf.metrics.seed_of(r[0], r[1], r[2]) for r in rows)


def test_cached_folds_run_again_identical_or_a_mismatch(folds):
    assert reproduce.main(FakeRunner([]), FakeFolds(), random.Random(2)) == 0
    ok, raw = last_run(folds)
    assert ok and (raw["checked"], raw["mismatch"]) == (2, 0)
    assert reproduce.main(FakeRunner([]), Retuned(), random.Random(2)) == 0
    ok, raw = last_run(folds)
    assert raw["mismatch"] == 2 and all(r["diff"][0].startswith("params:") for r in raw["results"])
