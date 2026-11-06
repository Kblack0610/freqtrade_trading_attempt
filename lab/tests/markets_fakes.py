"""A requests.Session stand-in for the venue adapter tests: answers GETs from recorded fixtures."""

import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "markets"


def fixture(name):
    return json.loads((FIX / name).read_text())


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    """Answers each GET from a queue of (status, fixture name or body) and records the URL and params sent."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        status, body = self.answers.pop(0)
        return Resp(status, fixture(body) if isinstance(body, str) else body)
