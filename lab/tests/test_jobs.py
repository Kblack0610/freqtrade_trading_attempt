"""The job registry: every job names a module that exists and can run as __main__, and a bad name starts nothing."""

import importlib.util
import os
import shlex
import sys

import pytest

from lab import __main__ as jobs_main

JOBS = jobs_main.load()


def steps():
    return [(name, step) for name, group in JOBS.items() for step in group]


@pytest.mark.parametrize("name,step", steps())
def test_step_module_exists_and_runs_as_main(name, step):
    module = shlex.split(step)[0]
    spec = importlib.util.find_spec(module)
    assert spec and spec.origin, f"{name}: no module {module}"
    assert '__name__ == "__main__"' in open(spec.origin).read(), f"{name}: {module} has no __main__ guard"


def test_unknown_job_exits_nonzero_and_runs_nothing(capsys):
    assert jobs_main.main(["run", "no-such-job"]) == 2
    assert "unknown job" in capsys.readouterr().err


def test_bad_usage_exits_nonzero():
    assert jobs_main.main([]) == 2
    assert jobs_main.main(["run"]) == 2


def test_list_prints_every_job(capsys):
    assert jobs_main.main(["list"]) == 0
    assert capsys.readouterr().out.split() == sorted(JOBS)


def test_every_step_runs_and_the_first_failure_is_the_exit_code(tmp_path, monkeypatch):
    calls = []

    def fake(argv):
        calls.append(argv)
        return {"x.a": 3, "x.b": 0, "x.c": 1}[argv[-1]]

    monkeypatch.setattr(jobs_main.subprocess, "call", fake)
    assert jobs_main.run("j", {"j": ["x.a", "x.b", "x.c"]}) == 3
    assert [c[-1] for c in calls] == ["x.a", "x.b", "x.c"]
    assert all(c[:2] == [sys.executable, "-m"] for c in calls)


def test_env_var_in_a_step_is_expanded_into_words(monkeypatch):
    monkeypatch.setenv("WF_EXCHANGES", "binanceus kraken")
    seen = []
    monkeypatch.setattr(jobs_main.subprocess, "call", lambda argv: seen.append(argv) or 0)
    jobs_main.run("walkforward", JOBS)
    assert seen[0][2:] == ["lab.snapshot", "fetch", "binanceus", "kraken"]


def test_entry_point_exits_2_for_an_unknown_job():
    import subprocess

    r = subprocess.run([sys.executable, "-m", "lab", "run", "nope"], capture_output=True, cwd=os.getcwd())
    assert r.returncode == 2
