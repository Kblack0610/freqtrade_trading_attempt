#!/usr/bin/env python3
"""Carry the nightly research loop's changes to the agent board, and a promotion to a roster PR.

Runs on the fleet machine after the nightly analysis (the agentctl runner `trader-research-sync`), from this repo's
origin/master, never from a checkout. It reads the events lab/loop.py wrote that are not yet reported, through
the deployed image (`kubectl exec deploy/trader-web -- python -m lab.loop pending`, the site's read-only role), then:

- climate changes: one agent-board row per night listing each strategy or mixture that entered or left a market
  climate's passing set (`notes ptask trader --agent add`);
- a promotion that enters: a home-config PR that appends its dry-run roster row to apps/freqtrade/roster.tsv and
  raises the StatefulSet's replicas to the row count, plus a board row to review it. Paper only: league.py refuses
  anything but key-less spot dry run, and the PR is opened, never merged (approved 2026-11-05, tr-17);
- a promotion that leaves: a board row, so an open roster PR for it is closed by a person;
- a monthly retune fit entering or leaving qualification: a review row with its parameter artifact. Parameters
  are never saved to a strategy or applied to a bot by this sync.

Each event is acked (`python -m lab.loop ack`, in deploy/freqtrade-watcher: the site's role cannot write) once its
row is filed, so a failed night retries the rest.

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
MOST_DETAILS = 40  # a board row past this many detail lines points at /book instead


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


def label(e: dict) -> str:
    who = e["name"] + (f" on {e['exchange']}" if e["exchange"] else "") + (f" ({e['variant']})" if e["variant"] else "")
    nums = "" if e["lo"] is None else f", annual {e['annual']:+.1%}, low end {e['lo']:+.1%}"
    return f"{e['change']} {e['cell']}: {e['kind']} {who}{nums}"


def climate_row(events: list[dict]) -> tuple[str, list[str]]:
    enters = sum(e["change"] == "enters" for e in events)
    day = min(e["at"] for e in events)[:10]
    text = (f"check(trader): research loop {day}: {enters} enter and {len(events) - enters} leave a market climate's "
            "passing set; read them on /book and say which to follow up #size/s")
    details = [label(e) for e in events[:MOST_DETAILS]]
    if len(events) > MOST_DETAILS:
        details.append(f"and {len(events) - MOST_DETAILS} more: https://trader.kblab.me/book")
    details.append(f"trial register at {events[-1]['trials']}: the interval is per cell, not corrected for the search")
    return text, details


READER = ("deploy/trader-web", "web")  # the site: DATABASE_URL is the read-only role
WRITER = ("deploy/freqtrade-watcher", "watcher")  # DATABASE_URL is the writer role; ack is an UPDATE


def kubectl_research(*args: str) -> str:
    target, container = WRITER if args[:1] == ("ack",) else READER
    cmd = ["kubectl", "--context", os.environ.get("KUBE_CONTEXT", "home-k3s"), "-n", "freqtrade", "exec",
           target, "-c", container, "--", "python", "-m", "lab.loop", *args]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode:
        why = (run.stderr.strip().splitlines() or ["no stderr"])[-1]
        raise SystemExit(f"lab.loop {args[0]} on {target} exited {run.returncode}: {why}")
    return run.stdout


def board(text: str, details: list[str]) -> None:
    cmd = ["notes", "ptask", "trader", "--agent", "add", text] + [a for d in details for a in ("--detail", d)]
    code = subprocess.run(cmd).returncode
    if code not in (0, 3):  # 3: the same open row is already there
        raise SystemExit(f"notes ptask add exited {code}")


def roster_pr(e: dict) -> str:
    """Open (or find) the home-config PR adding e's roster row; returns its URL, or a note when the row exists."""
    line, name = e["action"], e["action"].split()[0]
    branch = f"trader-roster-{name}"
    open_prs = call("GET", f"/repos/{REPO}/pulls?state=open&limit=50")
    mine = next((p for p in open_prs if p["head"]["ref"] == branch), None)
    if mine:
        return mine["html_url"]
    files = {path: call("GET", f"/repos/{REPO}/contents/{path}?ref={BASE}") for path in (ROSTER, STATEFULSET)}
    old = {path: base64.b64decode(f["content"]).decode() for path, f in files.items()}
    new = roster_change(old[ROSTER], old[STATEFULSET], line)
    if new is None:
        return f"{name} is already a roster row"
    title = f"freqtrade: dry-run league adds {name} ({e['name']} on {e['exchange']})"
    if call("GET", f"/repos/{REPO}/branches/{branch}", ok_404=True) is None:
        call("POST", f"/repos/{REPO}/contents", {
            "branch": BASE, "new_branch": branch, "message": title,
            "files": [{"operation": "update", "path": path, "sha": files[path]["sha"],
                       "content": base64.b64encode(text.encode()).decode()}
                      for path, text in zip((ROSTER, STATEFULSET), new)]})
    pr = call("POST", f"/repos/{REPO}/pulls", {
        "base": BASE, "head": branch, "title": title,
        "body": (f"Appends `{line}` to `{ROSTER}` and raises replicas to the row count. {e['name']} on {e['exchange']} "
                 "cleared every lab/suggest.toml [promote] gate in the nightly analysis "
                 "(https://trader.kblab.me/suggestions). Dry run only: lab/league.py refuses anything else. "
                 "Opened by Kblack0610/freqtrade_trading_attempt ci/research_sync.py; a person merges it.")})
    return pr["html_url"]


def main() -> int:
    dry = os.environ.get("DRY_RUN") == "1"
    events = json.loads(kubectl_research("pending"))
    if not events:
        print("research sync: nothing new")
        return 0
    done: list[int] = []
    climate = [e for e in events if e["signal"] == "climate"]
    if climate:
        text, details = climate_row(climate)
        print(text, *details, sep="\n  ")
        if not dry:
            board(text, details)
            done += [e["id"] for e in climate]
    for e in (e for e in events if e["signal"] == "promote"):
        if e["change"] == "enters":
            url = "(dry run)" if dry else roster_pr(e)
            text = (f"check(trader): review the paper roster PR promoting {e['name']} on {e['exchange']} to the "
                    "dry-run league #size/s")
            details = [url, f"roster row: {e['action']}"]
        else:
            text = (f"check(trader): {e['name']} on {e['exchange']} no longer clears the promote gates; close its "
                    "roster PR if it is still open #size/s")
            details = ["https://trader.kblab.me/suggestions"]
        print(text, *details, sep="\n  ")
        if not dry:
            board(text, details)
            done.append(e["id"])
    for e in (e for e in events if e["signal"] == "retune"):
        state = "review monthly retune" if e["change"] == "enters" else "monthly retune fit superseded or no longer qualifies"
        text = f"check(trader): {state} for {e['name']} on {e['exchange']} (fit {e['variant'][:12]}) #size/s"
        details = [e["action"], "Research only: review the fit before applying parameters; bots keep their current settings."]
        print(text, *details, sep="\n  ")
        if not dry:
            board(text, details)
            done.append(e["id"])
    if done:
        print(kubectl_research("ack", *map(str, done)).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
