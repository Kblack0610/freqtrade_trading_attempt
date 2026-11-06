"""Polymarket, read-only, both books: the US exchange (gateway.polymarket.us) and global prices (Gamma). No key.

A US resident may not trade global; we only read it (docs/markets.md, Polymarket global). Every market on either
book is binary, so each is one outcome, yes: on US the long side, on global the first outcome token, whose label
goes in the subtitle when it is not "Yes".

US: `/v1/markets` pages by offset, 500 at most, and its list carries bestBidQuote and bestAskQuote, so a sweep
needs no per-market /bbo call. Resolved markets come newest first with orderBy=updated_at; updatedAt is the
settled time we store, the nearest the list has. Global: `/markets/keyset` pages by after_cursor, 100 at most
(plain /markets refuses an offset past 2,100); closed markets come newest first with order=closedTime.
"""

import datetime as dt
import json
from typing import Iterator

from markets.api import Api, num, result_of, when
from markets.venue import Quote, Resolution


def _px(v) -> float | None:
    return num(v.get("value")) if isinstance(v, dict) else num(v)


def _long(m: dict) -> dict:
    return next(s for s in m["marketSides"] if s.get("long"))


def us_quote(m: dict, ts: dt.datetime) -> Quote:
    side = _long(m).get("description")
    return Quote(
        market_id=m["slug"], outcome="yes", ts=ts,
        event_id=m.get("gameId") or None, series_id=None,
        title=m.get("question") or "", subtitle=side if side and side != "Yes" else m.get("title") or None,
        market_type="binary", status=(m.get("status") or "").removeprefix("MARKET_STATUS_").lower(),
        open_time=when(m.get("startDate")), close_time=when(m.get("endDate")),
        bid=_px(m.get("bestBidQuote")), ask=_px(m.get("bestAskQuote")), last=_px(m.get("lastTradePrice")),
        bid_size=None, ask_size=None, volume=num(m.get("volumeNum")), open_interest=None,
    )


def us_resolution(m: dict) -> Resolution | None:
    if m.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    value = num(_long(m).get("price"))
    return Resolution(market_id=m["slug"], result=result_of(value), value=value, settled_at=when(m["updatedAt"]))


class PolymarketUS:
    name = "polymarket_us"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

    def _pages(self, params: dict) -> Iterator[tuple[list[dict], dt.datetime]]:
        offset = 0
        while True:
            markets = self.api.get("/v1/markets", {**params, "limit": self.api.limit, "offset": offset}).get("markets") or []
            yield markets, dt.datetime.now(dt.timezone.utc)
            if len(markets) < self.api.limit:
                return
            offset += self.api.limit
            self.api.sleep(self.api.gap)

    def open_markets(self) -> Iterator[list[Quote]]:
        for markets, ts in self._pages({"active": "true", "closed": "false"}):
            yield [us_quote(m, ts) for m in markets]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        for markets, _ in self._pages({"closed": "true", "orderBy": "updated_at", "orderDirection": "desc"}):
            for m in markets:
                if when(m["updatedAt"]) < since:
                    return
                if (r := us_resolution(m)) is not None:
                    yield r


def global_quote(m: dict, ts: dt.datetime) -> Quote:
    event = (m.get("events") or [{}])[0]
    first = (json.loads(m.get("outcomes") or "[]") or [None])[0]
    return Quote(
        market_id=m["conditionId"], outcome="yes", ts=ts,
        event_id=event.get("id"), series_id=event.get("seriesSlug"),
        title=m.get("question") or "", subtitle=m.get("groupItemTitle") or (first if first != "Yes" else None),
        market_type="binary", status="closed" if m.get("closed") else "active" if m.get("active") else "inactive",
        open_time=when(m.get("startDate")), close_time=when(m.get("endDate")),
        bid=num(m.get("bestBid")), ask=num(m.get("bestAsk")), last=num(m.get("lastTradePrice")),
        bid_size=None, ask_size=None, volume=num(m.get("volumeNum")), open_interest=None,
    )


def global_resolution(m: dict) -> Resolution | None:
    if m.get("umaResolutionStatus") != "resolved" or not m.get("closedTime"):
        return None
    value = num((json.loads(m.get("outcomePrices") or "[]") or [None])[0])
    return Resolution(market_id=m["conditionId"], result=result_of(value), value=value,
                      settled_at=when(m["closedTime"]))


class Polymarket:
    name = "polymarket"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

    def _pages(self, params: dict) -> Iterator[tuple[list[dict], dt.datetime]]:
        cursor = None
        while True:
            body = self.api.get("/markets/keyset", {**params, "limit": self.api.limit,
                                                    **({"after_cursor": cursor} if cursor else {})})
            yield body.get("markets") or [], dt.datetime.now(dt.timezone.utc)
            cursor = body.get("next_cursor")
            if not cursor or not body.get("markets"):
                return
            self.api.sleep(self.api.gap)

    def open_markets(self) -> Iterator[list[Quote]]:
        for markets, ts in self._pages({}):  # closed omitted means open
            yield [global_quote(m, ts) for m in markets]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        for markets, _ in self._pages({"closed": "true", "order": "closedTime", "ascending": "false"}):
            for m in markets:
                if m.get("closedTime") and when(m["closedTime"]) < since:
                    return
                if (r := global_resolution(m)) is not None:
                    yield r
