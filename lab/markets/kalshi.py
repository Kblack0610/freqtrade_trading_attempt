"""Kalshi, read-only, through the public market-data REST API (no key).

`/markets` pages by cursor, up to 1000 a page; combos (multivariate events) are left out with mve_filter=exclude.
Prices come as dollar strings and sizes as fixed-point strings (docs/markets.md, Kalshi). An empty side shows as
a bid of 0 or an ask of 1, and is stored as the venue sends it. Requests are paced and backed off by
lab/markets/api.py from `page_gap_s` and `budget_rps` in lab/markets.toml.

History (docs/markets.md, Kalshi): `/historical/cutoff` splits each record kind between the live endpoints and
`/historical/*`; a record lives on exactly one side. A settled-market walk reads the live side, then the historical
one. Candles of a market settled before `market_settled_ts` come from the historical side, at most 5000 a request;
a market's trades come from the historical side if any can predate `trades_created_ts`, and the live side if any
can follow it. Live candles carry `*_dollars` and `*_fp` fields, historical ones the bare names.
"""

import datetime as dt
from typing import Iterator

from lab.markets.api import Api, epoch_s, num, when
from lab.markets.venue import Bar, Quote, Resolution, Settled, Span, Trade

MAX_CANDLES = 5000


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
                      value=num(m.get("settlement_value_dollars")), settled_at=settled, volume=num(m.get("volume_fp")))


def _px(side: dict | None, k: str) -> float | None:
    side = side or {}
    return num(side.get(f"{k}_dollars", side.get(k)))


def bar(ticker: str, c: dict, period_s: int) -> Bar:
    price = c.get("price") or {}
    return Bar(market_id=ticker, ts=epoch_s(c["end_period_ts"]), period_s=period_s,
               bid=_px(c.get("yes_bid"), "close"), ask=_px(c.get("yes_ask"), "close"),
               open=_px(price, "open"), high=_px(price, "high"), low=_px(price, "low"), close=_px(price, "close"),
               volume=num(c.get("volume_fp", c.get("volume"))),
               open_interest=num(c.get("open_interest_fp", c.get("open_interest"))))


def trade(t: dict) -> Trade:
    return Trade(market_id=t["ticker"], trade_id=t["trade_id"], ts=when(t["created_time"]),
                 price=num(t["yes_price_dollars"]), count=num(t["count_fp"]),
                 taker_side=t.get("taker_outcome_side") or t.get("taker_side"))


class Kalshi:
    name = "kalshi"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

        self._cutoff = None

    def _walk(self, params: dict, path: str = "/markets", key: str = "markets",
              cursor: str | None = None) -> Iterator[tuple[list[dict], dt.datetime, str | None]]:
        """Each page with the cursor of the next one (None after the last)."""
        while True:
            body = self.api.get(path, {**params, "limit": self.api.limit, **({"cursor": cursor} if cursor else {})})
            cursor = body.get("cursor") or None
            yield body.get(key) or [], dt.datetime.now(dt.timezone.utc), cursor
            if not cursor:
                return

    def open_markets(self) -> Iterator[list[Quote]]:
        for markets, ts, _ in self._walk({"status": "open", "mve_filter": "exclude"}):
            yield [quote(m, ts) for m in markets]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        params = {"status": "settled", "mve_filter": "exclude", "min_settled_ts": int(since.timestamp())}
        for markets, _, _ in self._walk(params):
            for m in markets:
                if (r := resolution(m)) is not None:
                    yield r

    def cutoff(self, field: str) -> dt.datetime:
        if self._cutoff is None:
            self._cutoff = self.api.get("/historical/cutoff")
        return when(self._cutoff[field])

    def settled(self, cursor: str | None) -> Iterator[tuple[list[Settled], str | None]]:
        """The live side's settled markets, then the historical side's; the cursor names its side, `live:<c>`."""
        side, _, c = (cursor or "live:").partition(":")
        sides = [("live", "/markets", {"status": "settled"}), ("historical", "/historical/markets", {})]
        for name, path, params in sides[side == "historical":]:
            for markets, _, nxt in self._walk({**params, "mve_filter": "exclude"}, path,
                                              cursor=(c if name == side else None) or None):
                page = [Settled(quote(m, r.settled_at), r) for m in markets if (r := resolution(m)) is not None]
                yield page, f"{name}:{nxt}" if nxt else ("historical:" if name == "live" else None)

    def bars(self, span: Span, period_s: int) -> list[Bar]:
        historical = span.settled_at < self.cutoff("market_settled_ts")
        path = (f"/historical/markets/{span.market_id}/candlesticks" if historical
                else f"/series/{span.series_id or span.market_id.split('-')[0]}/markets/{span.market_id}/candlesticks")
        end = int(span.settled_at.timestamp())
        start = int(span.open_time.timestamp()) if span.open_time else end - period_s * MAX_CANDLES
        out = []
        while start < end:
            stop = min(end, start + period_s * MAX_CANDLES)
            body = self.api.get(path, {"start_ts": start, "end_ts": stop, "period_interval": period_s // 60})
            out += [bar(span.market_id, c, period_s) for c in body.get("candlesticks") or []]
            start = stop
        return out

    def trades(self, span: Span) -> Iterator[list[Trade]]:
        cut = self.cutoff("trades_created_ts")
        paths = (["/historical/trades"] if not span.open_time or span.open_time < cut else []) + \
                (["/markets/trades"] if span.settled_at >= cut else [])
        for path in paths:
            for rows, _, _ in self._walk({"ticker": span.market_id}, path, key="trades"):
                yield [trade(t) for t in rows]
