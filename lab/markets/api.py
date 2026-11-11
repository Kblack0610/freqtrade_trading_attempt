"""What every venue adapter shares: a paced JSON GET that backs off on 429 and 5xx, and the field parsers.

Api.get paces every request two ways (docs/markets.md, Rate limits). Its own gap starts at page_gap_s and is
additive-increase, multiplicative-decrease on the rate: a 429 or 5xx doubles the gap for the rest of the run, and
each success takes RECOVER_S off it, down to page_gap_s again. Then, when the venue's table sets budget_rps, the
venue's one budget shared by every process (lab/markets/budget.py) gives the request its slot. A retry waits the
Retry-After a venue sends, else page_gap_s * 2^n, at least 0.5 s, capped at 60 s, and holds the shared budget that
long so every other process pauses too. Every knob comes from the venue's table in lab/markets.toml.
"""

import datetime as dt
import email.utils
import time

import requests

from lab.markets.budget import Budget

RECOVER_S = 0.01  # a success takes this off a widened gap: 100 clean requests undo a doubling from 1 s
MAX_GAP_S = 30.0


def num(v) -> float | None:
    return None if v in (None, "") else float(v)


def when(v) -> dt.datetime | None:
    """An ISO-8601 string (Z, +00 or +00:00) or epoch milliseconds, as an aware UTC time; an out-of-range epoch is None."""
    if v in (None, "", "NA"):
        return None
    if isinstance(v, (int, float)):
        try:
            return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def epoch_s(v: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(v, dt.timezone.utc)


def result_of(value: float | None) -> str:
    """The yes outcome's payout as the Resolution.result word: yes, no, split (a partial payout) or void."""
    if value is None:
        return "void"
    return "yes" if value == 1 else "no" if value == 0 else "split"


def retry_after(r) -> float | None:
    """Seconds from a Retry-After header, in seconds or as an HTTP date; None when absent or unreadable."""
    v = (getattr(r, "headers", None) or {}).get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        return max(0.0, (email.utils.parsedate_to_datetime(v) - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class Api:
    def __init__(self, name: str, cfg: dict, session: requests.Session | None = None, sleep=None,
                 budget: Budget | None = None):
        self.name = name
        self.base = cfg["base_url"].rstrip("/")
        self.limit = int(cfg.get("page_limit", 1000))
        self.gap = float(cfg.get("page_gap_s", 0.5))
        self.pace = self.gap  # the gap in force: widened by a 429, walked back to self.gap by successes
        self.retries = int(cfg.get("retries", 6))
        self.timeout = float(cfg.get("timeout_s", 30))
        self.http = session or requests.Session()
        self.sleep = sleep or time.sleep
        self.budget = budget or (Budget(name, float(cfg["budget_rps"])) if cfg.get("budget_rps") else None)
        self.calls = 0

    def _turn(self):
        if self.calls and self.pace > 0:
            self.sleep(self.pace)
        if self.budget and (wait := self.budget.take()) > 0:
            self.sleep(wait)
        self.calls += 1

    def get(self, path: str, params: dict | None = None, label: str | None = None):
        """label stands in for path in the retry log line when the path itself is private."""
        for attempt in range(self.retries + 1):
            self._turn()
            r = self.http.get(f"{self.base}{path}", params=params or {}, timeout=self.timeout)
            if r.status_code != 429 and r.status_code < 500:
                r.raise_for_status()
                self.pace = max(self.gap, self.pace - RECOVER_S)
                return r.json()
            if attempt == self.retries:
                r.raise_for_status()
            self.pace = min(MAX_GAP_S, max(self.pace, 0.1) * 2)
            wait = retry_after(r)
            wait = min(60.0, max(self.gap, 0.5) * 2 ** (attempt + 1)) if wait is None else wait
            print(f"markets: {self.name}: HTTP {r.status_code} on {label or path}, retry in {wait:g}s, "
                  f"gap now {self.pace:g}s", flush=True)
            if self.budget:
                self.budget.hold(wait)
            self.sleep(wait)
        raise AssertionError("unreachable")
