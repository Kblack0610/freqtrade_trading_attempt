"""The job registry: every job names a module that exists and can run as __main__, and a bad name starts nothing."""

import importlib.util
import os
import shlex
import sys

import pytest

from lab import __main__ as jobs_main

JOBS = jobs_main.load()


def steps():
    return [(name, step) for name, job in JOBS.items() for step in job.steps]


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


class Exec(Exception):
    pass


@pytest.fixture
def steps_ran(monkeypatch):
    """Fake step processes: subprocess.call returns each module's code from `codes`; exec raises Exec(argv)."""
    calls, codes = [], {}

    def call(argv):
        calls.append(argv[-1])
        return codes[argv[-1]]

    def execv(path, argv):
        assert path == sys.executable and argv[:2] == [sys.executable, "-m"]
        calls.append(("exec", argv[-1]))
        raise Exec(argv)

    monkeypatch.setattr(jobs_main.subprocess, "call", call)
    monkeypatch.setattr(jobs_main.os, "execv", execv)
    return calls, codes


def test_every_step_runs_and_the_first_failure_is_the_exit_code(steps_ran):
    calls, codes = steps_ran
    codes.update({"x.a": 3, "x.b": 0, "x.c": 1})
    assert jobs_main.run("j", {"j": jobs_main.Job(["x.a", "x.b", "x.c"])}) == 3
    assert calls == ["x.a", "x.b", "x.c"]


def test_the_last_step_replaces_the_runner_when_every_step_before_it_passed(steps_ran):
    calls, codes = steps_ran
    codes.update({"x.a": 0})
    with pytest.raises(Exec):
        jobs_main.run("j", {"j": jobs_main.Job(["x.a", "x.b"])})
    assert calls == ["x.a", ("exec", "x.b")]


def test_a_one_step_job_is_execd(steps_ran):
    calls, _ = steps_ran
    with pytest.raises(Exec):
        jobs_main.run("league", JOBS)
    assert calls == [("exec", "lab.crypto.league")]


def test_stop_on_failure_skips_the_rest(steps_ran):
    calls, codes = steps_ran
    codes.update({"x.a": 4})
    assert jobs_main.run("j", {"j": jobs_main.Job(["x.a", "x.b"], stop_on_failure=True)}) == 4
    assert calls == ["x.a"]


def test_walkforward_stops_on_a_failed_fetch_and_expands_the_exchanges(steps_ran, monkeypatch):
    calls, codes = steps_ran
    monkeypatch.setenv("WF_EXCHANGES", "binanceus kraken")
    seen = []
    monkeypatch.setattr(jobs_main.subprocess, "call", lambda argv: seen.append(argv) or 1)
    assert jobs_main.run("walkforward", JOBS) == 1
    assert [a[2:] for a in seen] == [["lab.crypto.snapshot", "fetch", "binanceus", "kraken"]]


def test_a_recorder_failure_still_runs_paper_and_keeps_its_code(steps_ran):
    calls, codes = steps_ran
    codes.update({"lab.stages.recorder": 5, "lab.events.paper": 0})
    assert jobs_main.run("markets-recorder", JOBS) == 5
    assert calls == ["lab.stages.recorder", "lab.events.paper"]


def test_a_one_step_job_keeps_the_runner_pid(tmp_path):
    import subprocess

    (tmp_path / "pidjob.py").write_text('import os\nif __name__ == "__main__":\n    print(os.getpid())\n')
    code = ("import os, sys; from lab import __main__ as m; print(os.getpid(), flush=True); "
            "sys.exit(m.run('p', {'p': m.Job(['pidjob'])}))")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([os.getcwd(), str(tmp_path)])}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    runner, step = r.stdout.split()
    assert runner == step


def test_entry_point_exits_2_for_an_unknown_job():
    import subprocess

    r = subprocess.run([sys.executable, "-m", "lab", "run", "nope"], capture_output=True, cwd=os.getcwd())
    assert r.returncode == 2
