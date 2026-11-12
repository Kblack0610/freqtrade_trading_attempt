#!/usr/bin/env python3
"""Carry the nightly research loop's changes to the agent board.

Runs on the fleet machine after the nightly analysis (the agentctl runner `trader-research-sync`), from this repo's
origin/master, never from a checkout. It reads the events lab/loop.py wrote that are not yet reported, through
the deployed image (`kubectl exec deploy/trader-web -- python -m lab.loop pending`, the site's read-only role), then:

- climate changes: one agent-board row per night listing each strategy or mixture that entered or left a market
  climate's passing set (`notes ptask trader --agent add`);
- a promotion that enters or leaves: a board row. Forward testing is lab/shadow.py's shadow accounts, database rows
  with no pod; a pod is a person's pick through ci/cohort_roster.py, so this sync opens no roster PR;
- a monthly retune fit entering or leaving qualification: a review row with its parameter artifact. Parameters
  are never saved to a strategy or applied to a bot by this sync.

Each event is acked (`python -m lab.loop ack`, in deploy/freqtrade-watcher: the site's role cannot write) once its
row is filed, so a failed night retries the rest.

Env: KUBE_CONTEXT (default home-k3s), DRY_RUN=1 to print only.
"""
import json
import os
import subprocess
import sys

MOST_DETAILS = 40  # a board row past this many detail lines points at /book instead


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
        sys.stderr.write(run.stderr)  # the whole error for the runner's log; the exit line is its outcome
        why = (run.stderr.strip().splitlines() or ["no stderr"])[-1]
        raise SystemExit(f"lab.loop {args[0]} on {target} exited {run.returncode}: {why}")
    return run.stdout


def board(text: str, details: list[str]) -> None:
    cmd = ["notes", "ptask", "trader", "--agent", "add", text] + [a for d in details for a in ("--detail", d)]
    code = subprocess.run(cmd).returncode
    if code not in (0, 3):  # 3: the same open row is already there
        raise SystemExit(f"notes ptask add exited {code}")


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
            text = (f"check(trader): {e['name']} on {e['exchange']} cleared the promote gates; its shadow account "
                    "forward-tests it on /accounts, and a pod is a person's pick (ci/cohort_roster.py) #size/s")
            details = ["https://trader.kblab.me/accounts", f"roster row if picked: {e['action']}"]
        else:
            text = (f"check(trader): {e['name']} on {e['exchange']} no longer clears the promote gates; retire "
                    "its bot if a person rostered one #size/s")
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
