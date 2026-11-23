"""`python -m lab run <job>` runs a job by name from lab/jobs.toml; `python -m lab list` prints the names.

Every step is its own `python -m <module> [args]` process in the current directory, as a CronJob ran it before, so a
job's exit code, environment and imports are the module's own. Every step runs; the exit code is the first nonzero one.
The last step replaces this process when every step before it passed, so a one-step job is PID 1 in its pod and gets
SIGTERM itself (a Python PID 1 with no handler ignores it). A stop_on_failure job ends at its first failing step.
An unknown job exits 2 and starts nothing.
"""

import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

JOBS = Path(__file__).with_name("jobs.toml")


@dataclass(frozen=True)
class Job:
    steps: list[str]
    stop_on_failure: bool = False


def load(path: Path = JOBS) -> dict[str, Job]:
    jobs = {}
    for name, spec in tomllib.loads(path.read_text())["jobs"].items():
        jobs[name] = Job(spec) if isinstance(spec, list) else Job(spec["steps"], spec.get("stop_on_failure", False))
    return jobs


def argv_of(step: str) -> list[str]:
    return [sys.executable, "-m", *shlex.split(os.path.expandvars(step))]


def run(name: str, jobs: dict[str, Job]) -> int:
    job = jobs[name]
    *head, last = job.steps
    code = 0
    for step in head:
        rc = subprocess.call(argv_of(step))
        if rc and job.stop_on_failure:
            return rc
        code = code or rc
    if code:
        subprocess.call(argv_of(last))
        return code
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv_of(last))


def main(argv: list[str]) -> int:
    jobs = load()
    if argv == ["list"]:
        print("\n".join(sorted(jobs)))
        return 0
    if len(argv) != 2 or argv[0] != "run":
        print("usage: python -m lab run <job> | python -m lab list", file=sys.stderr)
        return 2
    if argv[1] not in jobs:
        print(f"unknown job {argv[1]!r}; known: {', '.join(sorted(jobs))}", file=sys.stderr)
        return 2
    return run(argv[1], jobs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
