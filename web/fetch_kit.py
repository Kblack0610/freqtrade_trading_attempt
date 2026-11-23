"""Download the pinned web-kit files into web/static/kit/ and check each against web/kit.lock.

The kit (design tokens, fonts, <kb-shell>) lives in the dotfiles repo; kit.lock pins one commit and a
sha256 per file, so a kit change reaches this site only when the lock is bumped. The Dockerfile runs this,
so an image build fails on a bad checksum. Local dev: `python web/fetch_kit.py` once.
"""
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
LOCK = HERE / "kit.lock"
DEST = HERE / "static" / "kit"
URL = "https://raw.githubusercontent.com/{repo}/{commit}/{path}/{name}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


TRIES = 4
BACKOFF_S = 5


def fetch(lock: dict, dest: Path = DEST, get=None, sleep=time.sleep) -> None:
    raw = get or (lambda url: urllib.request.urlopen(url, timeout=30).read())

    def get(url):
        # raw.githubusercontent.com answers 503 "max_conn reached" under load; that failed master's build once.
        for attempt in range(1, TRIES + 1):
            try:
                return raw(url)
            except (urllib.error.URLError, TimeoutError):
                if attempt == TRIES:
                    raise
                sleep(BACKOFF_S * attempt)

    dest.mkdir(parents=True, exist_ok=True)
    for name, want in lock["files"].items():
        data = get(URL.format(name=name, **lock))
        if sha256(data) != want:
            raise SystemExit(f"kit file {name} does not match web/kit.lock (got {sha256(data)}, want {want})")
        (dest / name).write_bytes(data)


if __name__ == "__main__":
    fetch(json.loads(LOCK.read_text()))
    print(f"web-kit {json.loads(LOCK.read_text())['commit'][:9]} -> {DEST}", file=sys.stderr)
