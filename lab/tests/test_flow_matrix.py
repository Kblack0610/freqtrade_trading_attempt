"""Every strategy in the book through every avenue of the trader flow (lab/crypto/flow_matrix.py).

FLOW_MATRIX=gate runs the flow_gate sample, one strategy per family, plus the negative controls; CI's test-flow
job runs it with `-m flow_gate` in FLOW_SHARDS parallel jobs, shard FLOW_SHARD taking every FLOW_SHARDS-th strategy of
the sample (the last shard, the smallest, also runs the controls). FLOW_MATRIX=full runs the whole book. Unset, only the
unit tests of the module's lists run. The matrix needs DATABASE_URL (a throwaway database) and the trader image's Freqtrade.
"""

import os
import shutil
from pathlib import Path

import pytest

from lab.crypto import cohorts, flow_matrix

MODE = os.environ.get("FLOW_MATRIX", "")
SHARD, SHARDS = int(os.environ.get("FLOW_SHARD", "0")), int(os.environ.get("FLOW_SHARDS", "1"))
FAMS = cohorts.families()
BOOK = [n for n in FAMS if n not in flow_matrix.research()]
SAMPLE = flow_matrix.sample(FAMS)
PLANTED = flow_matrix.REPO / "lab" / "tests" / "fixtures" / "strategies" / "PlantedLookahead.py"
CRASH = '''
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
# A class the scan finds but families.toml has lost: its backtest cell must fail, as /coverage shows it untagged.
UNTAGGED = next(n for n in BOOK if n not in SAMPLE and FAMS[n] == "trend")
# (strategy, avenue) -> what its fail reason holds
CONTROLS = {("PlantedLookahead", "bias"): "lookahead", (UNTAGGED, "backtest"): "families.toml",
            ("FlowCrash", "backtest"): "planted crash"}
EXTRA = ("PlantedLookahead", "FlowCrash")


def user_files() -> list[Path]:
    ud = flow_matrix.REPO / "user_data"
    return sorted(p.relative_to(ud) for p in ud.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_the_sample_is_the_first_class_of_each_family_outside_the_research():
    assert len(SAMPLE) == len(set(FAMS[n] for n in BOOK))
    assert all(FAMS[n] != FAMS[m] for i, n in enumerate(SAMPLE) for m in SAMPLE[i + 1:])
    assert not set(SAMPLE) & flow_matrix.research() and flow_matrix.research() <= set(FAMS)


def test_the_book_is_the_scan_plus_families_toml_without_the_research():
    found = [{"name": "Scanned"}, {"name": BOOK[0]}]
    assert flow_matrix.book(found, FAMS) == BOOK + ["Scanned"]


def test_a_bias_check_with_nothing_to_judge_is_a_skip_not_a_finding():
    assert flow_matrix.NOTHING_TO_JUDGE.search("only 0 signals in 20 days")
    assert flow_matrix.NOTHING_TO_JUDGE.search("ConfigurationError: The strategy defines invalid startup candle count of 0.")
    assert not flow_matrix.NOTHING_TO_JUDGE.search("OperationalException: No data found. Terminating.")


def test_the_summary_counts_each_avenue():
    rows = [{"avenue": "backtest", "status": "ok"}, {"avenue": "bias", "status": "fail"}, {"avenue": "bias", "status": "skip"}]
    got = flow_matrix.summary(rows)
    assert got["backtest"] == {"ok": 1, "skip": 0, "fail": 0} and got["bias"] == {"ok": 0, "skip": 1, "fail": 1}
    assert set(got) == set(flow_matrix.AVENUES)


def params():
    out = []
    for name in BOOK:
        marks = [pytest.mark.flow_gate] if name in SAMPLE else []
        out += [pytest.param(name, a, marks=marks, id=f"{name}-{a}") for a in flow_matrix.AVENUES]
    return out


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    if MODE not in ("gate", "full"):
        pytest.skip("FLOW_MATRIX not set to gate or full")
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    from lab.core import db
    from lab import migrate

    assert migrate.main() == 0
    names = SAMPLE[SHARD::SHARDS] + ([UNTAGGED, *EXTRA] if SHARD == SHARDS - 1 else []) if MODE == "gate" else None
    fams = {**{n: f for n, f in FAMS.items() if n != UNTAGGED}, "PlantedLookahead": "control", "FlowCrash": "control"}
    work = tmp_path_factory.mktemp("controls")
    shutil.copy(PLANTED, work / PLANTED.name)
    (work / "FlowCrash.py").write_text(CRASH)
    files = user_files()
    tables = ("strategies", "backtest_chunks", "bias_checks", "walkforward", "readiness", "book", "trials")
    with db.connect(autocommit=True) as conn:
        before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        runs = conn.execute("SELECT kind, count(*) FROM runs GROUP BY kind").fetchall()
        last = conn.execute("SELECT coalesce(max(id), 0) FROM runs").fetchone()[0]
        exit_code = flow_matrix.main(names, fams, tuple(work / f"{n}.py" for n in EXTRA))
        after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        new = conn.execute("SELECT kind, ok, raw FROM runs WHERE id > %s", (last,)).fetchall()
        schema = conn.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s",
                              (flow_matrix.SCHEMA,)).fetchone()[0]
        runs_after = dict(conn.execute("SELECT kind, count(*) FROM runs WHERE id <= %s GROUP BY kind", (last,)).fetchall())
    report = new[0][2] if len(new) == 1 else {}
    cells = {(c["strategy"], c["avenue"], c["exchange"]): c for c in report.get("cells", [])}
    return {"exit": exit_code, "before": before, "after": after, "new": new, "schema": schema, "runs": dict(runs),
            "runs_after": runs_after, "report": report, "cells": cells, "names": names, "files": (files, user_files())}


@pytest.mark.flow_gate
def test_the_run_is_recorded_and_isolated(matrix):
    """One runs row of kind 'flow_matrix' is all the run adds to the production tables (as test_calibration), and the
    real user_data gains no file (no results, candles or params)."""
    assert [k for k, *_ in matrix["new"]] == ["flow_matrix"], matrix["new"]
    assert matrix["new"][0][1] is True and matrix["exit"] == 0, matrix["report"].get("error") or matrix["report"].get("probe_error")
    assert matrix["after"] == matrix["before"]
    assert matrix["runs_after"] == matrix["runs"]
    assert matrix["schema"] == 0  # the scratch schema is dropped
    assert matrix["files"][0] == matrix["files"][1]


@pytest.mark.flow_gate
def test_every_cell_is_there_with_a_reason(matrix):
    """Every strategy on every avenue's exchanges has exactly one cell, ok, skip or fail, each with a reason."""
    from lab.crypto import analyze

    report = matrix["report"]
    names = matrix["names"] or flow_matrix.book(analyze.Cli().discover(), FAMS) + list(EXTRA)
    if matrix["names"] is None:  # the whole book is what the scan finds (test_every_active_family_member_is_discovered)
        assert report["strategies"] == len(names)
    want = {(n, a, e) for n in names for a in flow_matrix.AVENUES for e in report["exchanges"][a]}
    assert set(matrix["cells"]) == want
    assert len(report["cells"]) == flow_matrix.expected(report)
    assert all(c["status"] in ("ok", "skip", "fail") and c["reason"] for c in report["cells"])


@pytest.mark.flow_gate
@pytest.mark.parametrize("avenue", flow_matrix.AVENUES)
def test_every_avenue_runs(matrix, avenue):
    """Each avenue reaches a verdict for at least one strategy (ok, a bias found, or one check clean), so a broken stage cannot
    pass as strategy findings."""
    cells = [c for c in matrix["report"]["cells"] if c["avenue"] == avenue]
    assert any(c["status"] == "ok" or c["reason"].startswith("biased:") or flow_matrix.CLEAN in c["reason"]
               for c in cells), cells


@pytest.mark.flow_gate
@pytest.mark.parametrize("strategy,avenue", list(CONTROLS))
def test_negative_controls_fail(matrix, strategy, avenue):
    if matrix["names"] is not None and strategy not in matrix["names"]:
        pytest.skip("the controls run in the last shard")
    got = [c for c in matrix["report"]["cells"] if c["strategy"] == strategy and c["avenue"] == avenue]
    assert got and all(c["status"] == "fail" and CONTROLS[strategy, avenue] in c["reason"] for c in got), got


@pytest.mark.parametrize("strategy,avenue", params())
def test_cell(matrix, strategy, avenue):
    """The strategy ran on the avenue on every exchange it is on; a skip is a step the flow leaves out on purpose.
    A failing cell is reported as xfail with its reason: the strategy's defect, which the full run's record lists."""
    if matrix["names"] is not None and strategy not in matrix["names"]:
        pytest.skip("not in this shard of the gate sample")
    if (strategy, avenue) in CONTROLS:
        pytest.skip("a negative control: test_negative_controls_fail")
    got = [c for (s, a, _), c in matrix["cells"].items() if s == strategy and a == avenue]
    assert got, "no cell"
    bad = [f"{c['exchange']}: {c['reason']}" for c in got if c["status"] == "fail"]
    if bad:  # a finding about the strategy, filed as a follow-up row, not a broken flow
        pytest.xfail("; ".join(bad))
