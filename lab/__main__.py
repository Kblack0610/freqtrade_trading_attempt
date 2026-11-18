"""`python -m lab run <job>` runs a job by name from lab/jobs.toml; `python -m lab list` prints the names.

Every step is its own `python -m <module> [args]` process in the current directory, as a CronJob ran it before, so a
job's exit code, environment and imports are the module's own. Every step runs; the exit code is the first nonzero one.
An unknown job exits 2 and starts nothing.
"""

import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

JOBS = Path(__file__).with_name("jobs.toml")


def load(path: Path = JOBS) -> dict[str, list[str]]:
    return tomllib.loads(path.read_text())["jobs"]


def run(name: str, jobs: dict[str, list[str]]) -> int:
    code = 0
    for step in jobs[name]:
        argv = [sys.executable, "-m", *shlex.split(os.path.expandvars(step))]
        rc = subprocess.call(argv)
        code = code or rc
    return code


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
