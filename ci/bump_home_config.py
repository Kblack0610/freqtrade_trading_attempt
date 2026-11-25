#!/usr/bin/env python3
"""Open the home-config PR that pins a new trader and trader-ml image digest.

CI runs this after `publish` on each master push. It rewrites the `trader` and
`trader-ml` entries under `images:` in apps/freqtrade/kustomization.yaml (the
analysis job runs trader-ml, so both move together) through the Forgejo
API (no clone), pushes it as branch trader-sha-<7>, opens the PR, and closes
any older open trader-sha-* PR it supersedes, then squash-merges it once Forgejo
reports it mergeable and its head's checks green. That merge is the deploy:
Flux rolls the bots onto the new digest. home-config's master branch protection
requires its `checks / *` statuses, so a merge before they pass answers 405
"Not all required status checks successful" and nothing deploys (ar-5150).

Env: DIGEST and ML_DIGEST (sha256:...), GITHUB_SHA, SUBJECT (commit message),
FORGEJO_TOKEN (write:repository on home-config; no issue scope needed), DRY_RUN=1 to print only.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("FORGEJO_API", "https://git.kennethblack.me/api/v1")
REPO = "kblack0610/home-config"
FILE = "apps/freqtrade/kustomization.yaml"
BASE = "master"
PREFIX = "trader-sha-"
# Forgejo computes mergeability after the PR opens, and home-config's required
# checks report on the head a minute or two later (90 s on #1519, 2026-11-25; a
# cold runner pod adds a few minutes); the merge call answers 405 until both are
# in. Forgejo has no event a script can await for either, so the PR and its head
# status are polled at this cadence, bounded by MERGEABLE_TIMEOUT_S.
MERGEABLE_POLL_S = 5
MERGEABLE_TIMEOUT_S = 900
# Merges after ours that could push it out of the commits list read back.
BASE_COMMITS_CHECKED = 50
ON_BASE_TRIES = 3
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def bump(text, digest, tag, name="trader"):
    """Return text with image name's digest line set to digest  # tag."""
    if not DIGEST_RE.match(digest):
        raise ValueError(f"not a digest: {digest!r}")
    lines = text.split("\n")
    in_entry = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- name:"):
            in_entry = stripped == f"- name: {name}"
        elif in_entry and stripped.startswith("digest:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}digest: {digest}  # {tag}"
            return "\n".join(lines)
    raise ValueError(f"no digest line under '- name: {name}' in {FILE}")


def call(method, path, body=None, ok_404=False):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": "token " + os.environ["FORGEJO_TOKEN"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "trader-ci",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if ok_404 and e.code == 404:
            return None
        raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:500]}")
    return json.loads(raw) if raw else {}


def main():
    digest = os.environ["DIGEST"]
    tag = "sha-" + os.environ["GITHUB_SHA"][:7]
    subject = (os.environ.get("SUBJECT", "").strip().splitlines() or [tag])[0]
    branch = PREFIX + tag.removeprefix("sha-")
    title = f"freqtrade: trader {tag} ({subject})"

    current = call("GET", f"/repos/{REPO}/contents/{FILE}?ref={BASE}")
    old = base64.b64decode(current["content"]).decode()
    new = bump(bump(old, digest, tag), os.environ["ML_DIGEST"], tag, "trader-ml")
    if new == old:
        print(f"{FILE} already pins {digest}; nothing to do")
        return
    if os.environ.get("DRY_RUN") == "1":
        print(f"would open {branch}: {title}")
        for a, b in zip(old.split("\n"), new.split("\n")):
            if a != b:
                print(f"- {a}\n+ {b}")
        return

    if call("GET", f"/repos/{REPO}/branches/{branch}", ok_404=True) is None:
        call("PUT", f"/repos/{REPO}/contents/{FILE}", {
            "branch": BASE,
            "new_branch": branch,
            "sha": current["sha"],
            "content": base64.b64encode(new.encode()).decode(),
            "message": title,
        })

    open_prs = call("GET", f"/repos/{REPO}/pulls?state=open&limit=50")
    mine = next((p for p in open_prs if p["head"]["ref"] == branch), None)
    if mine is None:
        mine = call("POST", f"/repos/{REPO}/pulls", {
            "base": BASE,
            "head": branch,
            "title": title,
            "body": (
                f"Pins `ghcr.io/kblack0610/trader` to `{digest}` and `trader-ml` to `{os.environ['ML_DIGEST']}` ({tag}), built from "
                f"Kblack0610/freqtrade_trading_attempt@{tag.removeprefix('sha-')}. "
                "Opened by that repo's image workflow; it merges this PR itself, which restarts the bots on the new image."
            ),
        })
    print(f"PR #{mine['number']}: {mine.get('html_url', '')}")

    close_superseded(open_prs, branch, mine["number"], tag)
    merge(mine["number"])


def wait_mergeable(number):
    """Poll the PR until it is merged, or mergeable with its head's checks green, and return it.

    Exits non-zero if the PR is closed, a check on its head failed, or the timeout
    passed. The checks are the head's combined status, which reads "" or "pending"
    until every check has reported.
    """
    deadline = time.monotonic() + MERGEABLE_TIMEOUT_S
    while True:
        pr = call("GET", f"/repos/{REPO}/pulls/{number}")
        if pr.get("merged"):
            return pr
        if pr.get("state") == "closed":
            raise SystemExit(f"PR #{number} is {pr.get('state')}, not mergeable")
        head = pr["head"]["sha"]
        checks = call("GET", f"/repos/{REPO}/commits/{head}/status").get("state") or "none"
        if checks in ("failure", "error"):
            raise SystemExit(f"PR #{number}: home-config checks read {checks} on {head[:7]}, so it cannot merge")
        if pr.get("mergeable") and checks == "success":
            return pr
        if time.monotonic() >= deadline:
            raise SystemExit(f"PR #{number} not mergeable after {MERGEABLE_TIMEOUT_S}s "
                             f"(state {pr.get('state')}, mergeable {pr.get('mergeable')}, checks {checks})")
        time.sleep(MERGEABLE_POLL_S)


def merge(number):
    """Squash-merge the bump PR and delete its branch; exit non-zero if it did not merge.

    Forgejo answers the merge call 200 even when nothing merged, and a PR can read
    merged=true with a merge commit that never reached BASE, so the PR is read back
    and its merge_commit_sha is looked up on BASE.
    """
    pr = wait_mergeable(number)
    if not pr.get("merged"):
        call("POST", f"/repos/{REPO}/pulls/{number}/merge",
             {"Do": "squash", "delete_branch_after_merge": True})
        pr = call("GET", f"/repos/{REPO}/pulls/{number}")
    if not pr.get("merged"):
        raise SystemExit(f"PR #{number} did not merge (state {pr.get('state')}, mergeable {pr.get('mergeable')})")
    sha = pr.get("merge_commit_sha") or ""
    if not on_base(sha):
        raise SystemExit(f"PR #{number} reads merged but {sha[:7] or 'no merge commit'} is not on {BASE}")
    print(f"merged #{number} as {sha[:7]}")


def on_base(sha):
    """True when sha is among the newest BASE_COMMITS_CHECKED commits of home-config BASE."""
    if not sha:
        return False
    # Forgejo has no event for its commit list catching up with a merge, so the
    # list is re-read at the merge-poll cadence, bounded by ON_BASE_TRIES.
    for attempt in range(ON_BASE_TRIES):
        commits = call("GET", f"/repos/{REPO}/commits?sha={BASE}&limit={BASE_COMMITS_CHECKED}&stat=false&verification=false&files=false")
        if any(c.get("sha") == sha for c in commits):
            return True
        if attempt < ON_BASE_TRIES - 1:
            time.sleep(MERGEABLE_POLL_S)
    return False


def close_superseded(open_prs, branch, number, tag):
    """Close every other open trader-sha-* PR, noting its successor in the body.

    The note goes in the closing PATCH, not a comment: comments need the
    write:issue scope and FORGEJO_DEPLOY_TOKEN carries write:repository only.
    """
    for p in open_prs:
        ref = p["head"]["ref"]
        if ref.startswith(PREFIX) and ref != branch:
            body = ((p.get("body") or "").rstrip() + f"\n\nSuperseded by #{number} ({tag}).").lstrip()
            call("PATCH", f"/repos/{REPO}/pulls/{p['number']}", {"state": "closed", "body": body})
            call("DELETE", f"/repos/{REPO}/branches/{ref}", ok_404=True)
            print(f"closed #{p['number']} ({ref})")


if __name__ == "__main__":
    sys.exit(main())
