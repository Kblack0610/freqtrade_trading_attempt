#!/usr/bin/env python3
"""Open the home-config PR that pins a new trader image digest.

CI runs this after `publish` on each master push. It rewrites the `trader`
entry under `images:` in apps/freqtrade/kustomization.yaml through the Forgejo
API (no clone), pushes it as branch trader-sha-<7>, opens the PR, and closes
any older open trader-sha-* PR it supersedes. Merging the PR is the deploy.

Env: DIGEST (sha256:...), GITHUB_SHA, SUBJECT (commit message),
FORGEJO_TOKEN (write:repository on home-config), DRY_RUN=1 to print only.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = os.environ.get("FORGEJO_API", "https://git.kennethblack.me/api/v1")
REPO = "kblack0610/home-config"
FILE = "apps/freqtrade/kustomization.yaml"
BASE = "master"
PREFIX = "trader-sha-"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def bump(text, digest, tag):
    """Return text with the trader image's digest line set to digest  # tag."""
    if not DIGEST_RE.match(digest):
        raise ValueError(f"not a digest: {digest!r}")
    lines = text.split("\n")
    in_trader = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- name:"):
            in_trader = stripped == "- name: trader"
        elif in_trader and stripped.startswith("digest:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}digest: {digest}  # {tag}"
            return "\n".join(lines)
    raise ValueError(f"no digest line under '- name: trader' in {FILE}")


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
    new = bump(old, digest, tag)
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
                f"Pins `ghcr.io/kblack0610/trader` to `{digest}` ({tag}), built from "
                f"Kblack0610/freqtrade_trading_attempt@{tag.removeprefix('sha-')}. "
                "Opened by that repo's image workflow; merging it restarts the bots on the new image."
            ),
        })
    print(f"PR #{mine['number']}: {mine.get('html_url', '')}")

    for p in open_prs:
        ref = p["head"]["ref"]
        if ref.startswith(PREFIX) and ref != branch:
            call("POST", f"/repos/{REPO}/issues/{p['number']}/comments",
                 {"body": f"Superseded by #{mine['number']} ({tag})."})
            call("PATCH", f"/repos/{REPO}/pulls/{p['number']}", {"state": "closed"})
            call("DELETE", f"/repos/{REPO}/branches/{ref}", ok_404=True)
            print(f"closed #{p['number']} ({ref})")


if __name__ == "__main__":
    sys.exit(main())
