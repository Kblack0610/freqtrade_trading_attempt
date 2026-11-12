#!/usr/bin/env python3
"""Open the home-config PR that seeds a dry-run cohort's roster rows (lab/cohorts.toml, lab/cohorts.py).

Runs on the fleet machine, from this repo's origin/master. It asks the deployed image for the cohort's rows
(`kubectl exec deploy/trader-web -- python -m lab.cohorts pick <cohort>`), appends them to
apps/freqtrade/roster.tsv with the StatefulSet's replicas raised to the row count, and opens the PR. It never merges:
a roster row costs a PVC, so a person does. It seeds only an empty cohort: once the roster has any row of that cohort
it does nothing, since resizing or replacing a cohort is a human edit (the roster is append-only, README, Cohorts).

Usage: python ci/cohort_roster.py <cohort>
Env: FORGEJO_TOKEN (repo scope on home-config), KUBE_CONTEXT (default home-k3s), DRY_RUN=1 to print only.
"""
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bump_home_config import BASE, REPO, call  # noqa: E402

ROSTER = "apps/freqtrade/roster.tsv"
STATEFULSET = "apps/freqtrade/statefulset.yaml"
REPLICAS = re.compile(r"^(\s*replicas:\s*)(\d+)(.*)$", re.M)


def rows_of(roster: str) -> list[str]:
    return [line for line in roster.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def roster_change(roster: str, statefulset: str, line: str) -> tuple[str, str] | None:
    """Pure: roster.tsv with line appended and statefulset.yaml with replicas set to the new row count; None when
    a row of that bot name is already there. Refuses a StatefulSet whose replicas already disagree with the roster."""
    name = line.split()[0]
    rows = rows_of(roster)
    if any(r.split()[0] == name for r in rows):
        return None
    found = REPLICAS.findall(statefulset)
    if len(found) != 1:
        raise ValueError(f"{STATEFULSET}: expected one replicas line, found {len(found)}")
    if int(found[0][1]) != len(rows):
        raise ValueError(f"{STATEFULSET} has replicas {found[0][1]} for {len(rows)} roster rows; fix that first")
    return (roster.rstrip("\n") + "\n" + line + "\n",
            REPLICAS.sub(lambda m: f"{m[1]}{len(rows) + 1}{m[3]}", statefulset, count=1))


def in_cohort(roster: str, cohort: str) -> bool:
    return any(len(r.split()) == 5 and r.split()[3] == cohort for r in rows_of(roster))


def runs(row: str) -> tuple[str, str, str]:
    """(exchange, strategy, overlay) of a roster row; a 3-column row runs the base overlay."""
    f = row.split()
    return f[1], f[2], f[4] if len(f) == 5 else "base"


def seed(roster: str, statefulset: str, cohort: str, lines: list[str]) -> tuple[str, str] | None:
    """Pure: roster.tsv and statefulset.yaml with every line appended; None when the cohort is already in the roster
    or no line is new. A row of a bot name already there is skipped (roster_change), and so is one that
    would run a strategy, exchange and overlay a bot already runs: a second bot would make the same trades."""
    if in_cohort(roster, cohort):
        return None
    running = {runs(r) for r in rows_of(roster)}
    out = None
    for line in lines:
        if runs(line) not in running:
            out = roster_change(*(out or (roster, statefulset)), line) or out
    return out


def picks(cohort: str) -> list[str]:
    cmd = ["kubectl", "--context", os.environ.get("KUBE_CONTEXT", "home-k3s"), "-n", "freqtrade", "exec",
           "deploy/trader-web", "-c", "web", "--", "python", "-m", "lab.cohorts", "pick", cohort]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    cohort = argv[0]
    lines = picks(cohort)
    if not lines:
        print(f"cohort {cohort}: the analysis tables have no strategy that qualifies yet")
        return 0
    branch = f"trader-cohort-{cohort}"
    files = {path: call("GET", f"/repos/{REPO}/contents/{path}?ref={BASE}") for path in (ROSTER, STATEFULSET)}
    old = {path: base64.b64decode(f["content"]).decode() for path, f in files.items()}
    new = seed(old[ROSTER], old[STATEFULSET], cohort, lines)
    if new is None:
        print(f"cohort {cohort}: already in the roster (or nothing new); a person resizes it")
        return 0
    title = f"freqtrade: seed the {cohort} dry-run cohort ({len(lines)} bots)"
    if os.environ.get("DRY_RUN") == "1":
        print(title, *lines, sep="\n  ")
        return 0
    open_prs = call("GET", f"/repos/{REPO}/pulls?state=open&limit=50")
    mine = next((p for p in open_prs if p["head"]["ref"] == branch), None)
    if mine:
        print(f"already open: {mine['html_url']}")
        return 0
    if call("GET", f"/repos/{REPO}/branches/{branch}", ok_404=True) is None:
        call("POST", f"/repos/{REPO}/contents", {
            "branch": BASE, "new_branch": branch, "message": title,
            "files": [{"operation": "update", "path": path, "sha": files[path]["sha"],
                       "content": base64.b64encode(text.encode()).decode()}
                      for path, text in zip((ROSTER, STATEFULSET), new)]})
    pr = call("POST", f"/repos/{REPO}/pulls", {
        "base": BASE, "head": branch, "title": title,
        "body": (f"Appends {len(lines)} rows of the `{cohort}` cohort to `{ROSTER}` and raises replicas to the row "
                 f"count. The rows are what `[{cohort}]` in lab/cohorts.toml picks from the analysis tables (README, "
                 "Cohorts); the promote gates are ignored, the backtests and mixtures behind them are counted "
                 "trials. Dry run only: lab/league.py refuses anything else. Opened by "
                 "Kblack0610/freqtrade_trading_attempt ci/cohort_roster.py; a person merges it.\n\n"
                 + "\n".join(f"    {line}" for line in lines))})
    print(pr["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
