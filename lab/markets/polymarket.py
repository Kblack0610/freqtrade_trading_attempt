"""Polymarket, read-only, both books: the US exchange (gateway.polymarket.us) and global prices (Gamma). No key.

A US resident may not trade global; we only read it (docs/markets.md, Polymarket global). Every market on either
book is binary, so each is one outcome, yes: on US the long side, on global the first outcome token, whose label
goes in the subtitle when it is not "Yes".

US: `/v1/markets` pages by offset, 500 at most, and its list carries bestBidQuote and bestAskQuote, so a sweep
needs no per-market /bbo call. Resolved markets come newest first with orderBy=updated_at; updatedAt is the
settled time we store, the nearest the list has. Global: `/markets/keyset` pages by after_cursor, 100 at most
(plain /markets refuses an offset past 2,100); closed markets come newest first with order=closedTime.

History: US `/v1/price-history` takes the slug as `symbol` and returns longPrice (the yes ask) and shortPrice (the
no ask, 1 - the yes bid) per `fidelity` minutes; the venue keeps 45 days. Global `/v2/prices-history` on the data
host takes the yes token id, not the condition id, so each market's token is looked up on Gamma first; its `price`
per bucket goes in `close`, and it pages by cursor.
"""

import datetime as dt
import json
from typing import Iterator

from lab.markets.api import Api, epoch_s, num, result_of, when
from lab.markets.venue import Bar, Quote, Resolution, Settled, Span

DATA_API = "https://data-api.polymarket.com"


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


def _span(span: Span) -> tuple[int, int]:
    end = int(span.settled_at.timestamp())
    return (int(span.open_time.timestamp()) if span.open_time else end - 45 * 86400), end


class PolymarketUS:
    name = "polymarket_us"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

    def _pages(self, params: dict, offset: int = 0) -> Iterator[tuple[list[dict], dt.datetime]]:
        while True:
            markets = self.api.get("/v1/markets", {**params, "limit": self.api.limit, "offset": offset}).get("markets") or []
            yield markets, dt.datetime.now(dt.timezone.utc)
            if len(markets) < self.api.limit:
                return
            offset += self.api.limit
            self.api.sleep(self.api.gap)

    def settled(self, cursor: str | None) -> Iterator[tuple[list[Settled], str | None]]:
        """Offset paging, so a market resolving mid-walk shifts the list down: a page may repeat, none is skipped."""
        offset = int(cursor or 0)
        for markets, _ in self._pages({"closed": "true", "orderBy": "updated_at", "orderDirection": "desc"}, offset):
            offset += len(markets)
            page = [Settled(us_quote(m, r.settled_at), r) for m in markets if (r := us_resolution(m)) is not None]
            yield page, str(offset) if len(markets) == self.api.limit else None

    def bars(self, span: Span, period_s: int) -> list[Bar]:
        start, end = _span(span)
        body = self.api.get("/v1/price-history", {"symbol": span.market_id, "timestamp.startTimestamp": start,
                                                  "timestamp.endTimestamp": end, "fidelity": period_s // 60})
        return [Bar(market_id=span.market_id, ts=epoch_s(h["timestamp"]), period_s=period_s,
                    bid=None if h.get("shortPrice") is None else round(1 - h["shortPrice"], 6),
                    ask=num(h.get("longPrice")), open=None, high=None, low=None, close=None, volume=None,
                    open_interest=None) for h in body.get("history") or []]

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
                      settled_at=when(m["closedTime"]), volume=num(m.get("volumeNum")))


class Polymarket:
    name = "polymarket"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)
        self.data = Api(self.name, {**cfg, "base_url": DATA_API}, session, sleep)

    def _walk(self, params: dict, cursor: str | None = None) -> Iterator[tuple[list[dict], dt.datetime, str | None]]:
        while True:
            body = self.api.get("/markets/keyset", {**params, "limit": self.api.limit,
                                                    **({"after_cursor": cursor} if cursor else {})})
            markets = body.get("markets") or []
            cursor = body.get("next_cursor") if markets else None
            yield markets, dt.datetime.now(dt.timezone.utc), cursor or None
            if not cursor:
                return
            self.api.sleep(self.api.gap)

    def open_markets(self) -> Iterator[list[Quote]]:
        for markets, ts, _ in self._walk({}):  # closed omitted means open
            yield [global_quote(m, ts) for m in markets]

    def settled(self, cursor: str | None) -> Iterator[tuple[list[Settled], str | None]]:
        for markets, _, nxt in self._walk({"closed": "true", "order": "closedTime", "ascending": "false"}, cursor):
            yield [Settled(global_quote(m, r.settled_at), r) for m in markets
                   if (r := global_resolution(m)) is not None], nxt

    def bars(self, span: Span, period_s: int) -> list[Bar]:
        found = self.api.get("/markets", {"condition_ids": span.market_id, "closed": "true"})
        tokens = json.loads(found[0].get("clobTokenIds") or "[]") if found else []
        if not tokens:
            return []
        start, end = _span(span)
        params, out = {"token_id": tokens[0], "start": start, "end": end, "bucket_seconds": period_s}, []
        while True:
            self.api.sleep(self.api.gap)
            body = self.data.get("/v2/prices-history", params)
            out += [Bar(market_id=span.market_id, ts=epoch_s(h["timestamp"]), period_s=period_s, bid=None, ask=None,
                        open=None, high=None, low=None, close=num(h.get("price")), volume=None, open_interest=None)
                    for h in body.get("data") or []]
            page = body.get("pagination") or {}
            if not page.get("has_more") or not page.get("next_cursor"):
                return out
            params = {**params, "cursor": page["next_cursor"]}

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        for markets, _, _ in self._walk({"closed": "true", "order": "closedTime", "ascending": "false"}):
            for m in markets:
                if m.get("closedTime") and when(m["closedTime"]) < since:
                    return
                if (r := global_resolution(m)) is not None:
                    yield r
