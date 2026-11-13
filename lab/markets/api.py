"""The one paced client every third-party HTTP call in lab/ goes through, and the venue adapters' field parsers.

Api.request paces every request two ways (docs/markets.md, Rate limits). Its own gap starts at page_gap_s and is
additive-increase, multiplicative-decrease on the rate: a retried 429 or 5xx doubles the gap for the rest of the
run, and each success takes RECOVER_S off it, down to page_gap_s again. Then, when the provider's table sets
budget_rps, the provider's one budget shared by every process (lab/markets/budget.py) gives the request its slot.
A retry waits the Retry-After the provider sends, else page_gap_s * 2^n, at least 0.5 s, capped at 60 s, and holds
the shared budget that long so every other process pauses too. Every knob comes from the provider's table in the
toml its caller reads (lab/markets.toml, lab/broker.toml, lab/wallet.toml), beside its published limit.

What is retried depends on the method. A 429 means the provider did not act on the request, so every method is
retried on it. A 5xx is retried only for an idempotent method (IDEMPOTENT): a POST or PATCH that drew a 5xx may
have been carried out, an order placed, so it is returned to the caller at once. A timeout or dropped connection
is retried the same way for an idempotent method, after page_gap_s * 2^n, and raised at once for a write, for the
same reason; it says nothing about the rate, so it neither widens the gap nor holds the shared budget. params and headers may be callables, rebuilt on every
attempt, so a signature over a timestamp is fresh on each retry; body may be a callable too, called after params and
before headers, so a nonce in the body (Kraken) is fresh on each retry.
"""

import datetime as dt
import email.utils
import threading
import time

import requests

from lab.markets.budget import Budget

RECOVER_S = 0.01  # a success takes this off a widened gap: 100 clean requests undo a doubling from 1 s
MAX_GAP_S = 30.0
# RFC 9110 9.2.2: sending one of these twice has the effect of sending it once. DELETE is in because the only DELETEs
# lab/ sends cancel an order by its id: a repeat finds it canceled already and cannot place or grow anything.
IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


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
        self.pace = self.gap  # the gap in force: widened by a retried 429, walked back to self.gap by successes
        self.retries = int(cfg.get("retries", 6))
        self.timeout = float(cfg.get("timeout_s", 30))
        self.http = session or requests.Session()
        self.sleep = sleep or time.sleep
        self.budget = budget or (Budget(name, float(cfg["budget_rps"])) if cfg.get("budget_rps") else None)
        self.calls = 0
        self._turns = threading.Lock()  # threads sharing one Api take their turns one at a time

    def _turn(self):
        with self._turns:
            if self.calls and self.pace > 0:
                self.sleep(self.pace)
            if self.budget and (wait := self.budget.take()) > 0:
                self.sleep(wait)
            self.calls += 1

    def request(self, method: str, path: str, params=None, body=None, headers=None,
                label: str | None = None) -> requests.Response:
        """The provider's last response, whatever its status; the caller reads it and raises its own error.

        params is a dict or a callable returning one; headers is a dict or a callable taking that attempt's params
        and returning one. Both are called before every attempt, params first, so headers can sign them. label
        stands in for path in the retry log line when the path itself is private; nothing else is ever logged.
        """
        method = method.upper()
        for attempt in range(self.retries + 1):
            self._turn()
            q = params() if callable(params) else params
            b = body() if callable(body) else body
            try:
                r = self.http.request(method, f"{self.base}{path}", params=q, json=b,
                                      headers=headers(q) if callable(headers) else headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if method not in IDEMPOTENT or attempt == self.retries:
                    raise
                wait = min(60.0, max(self.gap, 0.5) * 2 ** (attempt + 1))
                print(f"markets: {self.name}: {type(e).__name__} on {method} {label or path}, retry in {wait:g}s",
                      flush=True)
                self.sleep(wait)
                continue
            retry = r.status_code == 429 or (r.status_code >= 500 and method in IDEMPOTENT)
            if not retry or attempt == self.retries:
                if r.status_code < 400:
                    self.pace = max(self.gap, self.pace - RECOVER_S)
                return r
            self.pace = min(MAX_GAP_S, max(self.pace, 0.1) * 2)
            wait = retry_after(r)
            wait = min(60.0, max(self.gap, 0.5) * 2 ** (attempt + 1)) if wait is None else wait
            print(f"markets: {self.name}: HTTP {r.status_code} on {method} {label or path}, retry in {wait:g}s, "
                  f"gap now {self.pace:g}s", flush=True)
            if self.budget:
                self.budget.hold(wait)
            self.sleep(wait)
        raise AssertionError("unreachable")

    def get(self, path: str, params: dict | None = None, label: str | None = None):
        """A JSON GET; a 4xx, or a 429 or 5xx past the last retry, raises through raise_for_status."""
        r = self.request("GET", path, params or {}, label=label)
        r.raise_for_status()
        return r.json()
