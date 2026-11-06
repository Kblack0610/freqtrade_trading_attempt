"""Kalshi, read-only, through the public market-data REST API (no key).

`/markets` pages by cursor, up to 1000 a page; combos (multivariate events) are left out with mve_filter=exclude.
Prices come as dollar strings and sizes as fixed-point strings (docs/markets.md, Kalshi). An empty side shows as
a bid of 0 or an ask of 1, and is stored as the venue sends it. Pages are spaced by `page_gap_s` from
lab/markets.toml; markets/api.py backs off on a 429.
"""

import datetime as dt
from typing import Iterator

from markets.api import Api, num, when
from markets.venue import Quote, Resolution


def quote(m: dict, ts: dt.datetime) -> Quote:
    event = m.get("event_ticker") or None
    return Quote(
        market_id=m["ticker"], outcome="yes", ts=ts,
        event_id=event, series_id=event.split("-")[0] if event else None,
        title=m.get("title") or "", subtitle=m.get("yes_sub_title") or None,
        market_type=m.get("market_type") or "binary", status=m.get("status") or "",
        open_time=when(m.get("open_time")), close_time=when(m.get("close_time")),
        bid=num(m.get("yes_bid_dollars")), ask=num(m.get("yes_ask_dollars")), last=num(m.get("last_price_dollars")),
        bid_size=num(m.get("yes_bid_size_fp")), ask_size=num(m.get("yes_ask_size_fp")),
        volume=num(m.get("volume_fp")), open_interest=num(m.get("open_interest_fp")),
    )


def resolution(m: dict) -> Resolution | None:
    settled = when(m.get("settlement_ts"))
    if not settled:
        return None
    return Resolution(market_id=m["ticker"], result=m.get("result") or "void",
                      value=num(m.get("settlement_value_dollars")), settled_at=settled)


class Kalshi:
    name = "kalshi"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

    def _pages(self, params: dict) -> Iterator[tuple[list[dict], dt.datetime]]:
        cursor = None
        while True:
            body = self.api.get("/markets", {**params, "limit": self.api.limit, **({"cursor": cursor} if cursor else {})})
            yield body.get("markets") or [], dt.datetime.now(dt.timezone.utc)
            cursor = body.get("cursor")
            if not cursor:
                return
            self.api.sleep(self.api.gap)

    def open_markets(self) -> Iterator[list[Quote]]:
        for markets, ts in self._pages({"status": "open", "mve_filter": "exclude"}):
            yield [quote(m, ts) for m in markets]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        params = {"status": "settled", "mve_filter": "exclude", "min_settled_ts": int(since.timestamp())}
        for markets, _ in self._pages(params):
            for m in markets:
                if (r := resolution(m)) is not None:
                    yield r
