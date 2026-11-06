"""Kalshi, read-only, through the public market-data REST API (no key).

`/markets` pages by cursor, up to 1000 a page; combos (multivariate events) are left out with mve_filter=exclude.
Prices come as dollar strings and sizes as fixed-point strings (docs/markets.md, Kalshi). An empty side shows as
a bid of 0 or an ask of 1, and is stored as the venue sends it. A 429 carries no Retry-After, so it backs off
exponentially; pages are spaced by `page_gap_s` from lab/markets.toml.
"""

import datetime as dt
import time
from typing import Iterator

import requests

from markets.venue import Quote, Resolution


def _f(v) -> float | None:
    return None if v in (None, "") else float(v)


def _t(v) -> dt.datetime | None:
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def quote(m: dict, ts: dt.datetime) -> Quote:
    event = m.get("event_ticker") or None
    return Quote(
        market_id=m["ticker"], outcome="yes", ts=ts,
        event_id=event, series_id=event.split("-")[0] if event else None,
        title=m.get("title") or "", subtitle=m.get("yes_sub_title") or None,
        market_type=m.get("market_type") or "binary", status=m.get("status") or "",
        open_time=_t(m.get("open_time")), close_time=_t(m.get("close_time")),
        bid=_f(m.get("yes_bid_dollars")), ask=_f(m.get("yes_ask_dollars")), last=_f(m.get("last_price_dollars")),
        bid_size=_f(m.get("yes_bid_size_fp")), ask_size=_f(m.get("yes_ask_size_fp")),
        volume=_f(m.get("volume_fp")), open_interest=_f(m.get("open_interest_fp")),
    )


def resolution(m: dict) -> Resolution | None:
    settled = _t(m.get("settlement_ts"))
    if not settled:
        return None
    return Resolution(market_id=m["ticker"], result=m.get("result") or "void",
                      value=_f(m.get("settlement_value_dollars")), settled_at=settled)


class Kalshi:
    name = "kalshi"

    def __init__(self, cfg: dict, session: requests.Session | None = None, sleep=time.sleep):
        self.base = cfg["base_url"].rstrip("/")
        self.limit = int(cfg.get("page_limit", 1000))
        self.gap = float(cfg.get("page_gap_s", 0.5))
        self.retries = int(cfg.get("retries", 6))
        self.timeout = float(cfg.get("timeout_s", 30))
        self.http = session or requests.Session()
        self.sleep = sleep

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(self.retries + 1):
            r = self.http.get(f"{self.base}{path}", params=params, timeout=self.timeout)
            if r.status_code != 429 and r.status_code < 500:
                r.raise_for_status()
                return r.json()
            if attempt == self.retries:
                r.raise_for_status()
            wait = min(60.0, self.gap * 2 ** (attempt + 1))
            print(f"markets: kalshi: HTTP {r.status_code} on {path}, retry in {wait:g}s", flush=True)
            self.sleep(wait)
        raise AssertionError("unreachable")

    def _pages(self, params: dict) -> Iterator[tuple[list[dict], dt.datetime]]:
        cursor = None
        while True:
            body = self._get("/markets", {**params, "limit": self.limit, **({"cursor": cursor} if cursor else {})})
            yield body.get("markets") or [], dt.datetime.now(dt.timezone.utc)
            cursor = body.get("cursor")
            if not cursor:
                return
            self.sleep(self.gap)

    def open_markets(self) -> Iterator[list[Quote]]:
        for markets, ts in self._pages({"status": "open", "mve_filter": "exclude"}):
            yield [quote(m, ts) for m in markets]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        params = {"status": "settled", "mve_filter": "exclude", "min_settled_ts": int(since.timestamp())}
        for markets, _ in self._pages(params):
            for m in markets:
                if (r := resolution(m)) is not None:
                    yield r
