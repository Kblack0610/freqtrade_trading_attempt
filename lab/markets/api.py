"""What every venue adapter shares: a JSON GET that backs off on 429 and 5xx, and the field parsers.

Venue pages carry no Retry-After we can rely on (Kalshi sends none), so a retry waits page_gap_s * 2^n, at least
0.5 s, capped at 60 s. Every knob comes from the venue's table in lab/markets.toml.
"""

import datetime as dt
import time

import requests


def num(v) -> float | None:
    return None if v in (None, "") else float(v)


def when(v) -> dt.datetime | None:
    """An ISO-8601 string (Z, +00 or +00:00) or epoch milliseconds, as an aware UTC time."""
    if v in (None, "", "NA"):
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc)
    t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def result_of(value: float | None) -> str:
    """The yes outcome's payout as the Resolution.result word: yes, no, split (a partial payout) or void."""
    if value is None:
        return "void"
    return "yes" if value == 1 else "no" if value == 0 else "split"


class Api:
    def __init__(self, name: str, cfg: dict, session: requests.Session | None = None, sleep=None):
        self.name = name
        self.base = cfg["base_url"].rstrip("/")
        self.limit = int(cfg.get("page_limit", 1000))
        self.gap = float(cfg.get("page_gap_s", 0.5))
        self.retries = int(cfg.get("retries", 6))
        self.timeout = float(cfg.get("timeout_s", 30))
        self.http = session or requests.Session()
        self.sleep = sleep or time.sleep

    def get(self, path: str, params: dict | None = None):
        for attempt in range(self.retries + 1):
            r = self.http.get(f"{self.base}{path}", params=params or {}, timeout=self.timeout)
            if r.status_code != 429 and r.status_code < 500:
                r.raise_for_status()
                return r.json()
            if attempt == self.retries:
                r.raise_for_status()
            wait = min(60.0, max(self.gap, 0.5) * 2 ** (attempt + 1))
            print(f"markets: {self.name}: HTTP {r.status_code} on {path}, retry in {wait:g}s", flush=True)
            self.sleep(wait)
        raise AssertionError("unreachable")
