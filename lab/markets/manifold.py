"""Manifold, read-only and play money (mana), through `/v0/search-markets` (no key, 500 requests/min per IP).

Binary markets only for now: a multiple-choice market prices each answer, and the list does not carry them.
Manifold is an AMM with no order book, so its probability is stored as `last` and bid and ask stay empty; its
prices are not money prices, so a gap against a money venue is not an arbitrage (docs/markets.md, Manifold).
Open markets page newest first by the beforeTime cursor. Resolved ones come newest first by resolve-date, which
pages by offset only, and the offset stops at 1,000.
"""

import datetime as dt
from typing import Iterator

from markets.api import Api, num, result_of, when
from markets.venue import Quote, Resolution

MAX_OFFSET = 1000


def quote(m: dict, ts: dt.datetime) -> Quote:
    return Quote(
        market_id=m["id"], outcome="yes", ts=ts, event_id=None, series_id=None,
        title=m.get("question") or "", subtitle=None, market_type="binary",
        status="resolved" if m.get("isResolved") else "open",
        open_time=when(m.get("createdTime")), close_time=when(m.get("closeTime")),
        bid=None, ask=None, last=num(m.get("probability")),
        bid_size=None, ask_size=None, volume=num(m.get("volume")), open_interest=None,
    )


def resolution(m: dict) -> Resolution | None:
    word, settled = m.get("resolution"), when(m.get("resolutionTime"))
    if not word or not settled:
        return None
    value = {"YES": 1.0, "NO": 0.0, "MKT": num(m.get("resolutionProbability"))}.get(word)  # CANCEL pays back
    return Resolution(market_id=m["id"], result=result_of(value), value=value, settled_at=settled)


class Manifold:
    name = "manifold"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

    def _search(self, params: dict) -> list[dict]:
        return self.api.get("/v0/search-markets", {"contractType": "BINARY", "limit": self.api.limit, **params})

    def open_markets(self) -> Iterator[list[Quote]]:
        before = None
        while True:
            markets = self._search({"sort": "newest", "filter": "open", **({"beforeTime": before} if before else {})})
            ts = dt.datetime.now(dt.timezone.utc)
            yield [quote(m, ts) for m in markets]
            if len(markets) < self.api.limit:
                return
            before = markets[-1]["createdTime"]
            self.api.sleep(self.api.gap)

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        offset = 0
        while offset <= MAX_OFFSET:
            markets = self._search({"sort": "resolve-date", "filter": "resolved", "offset": offset})
            for m in markets:
                if (r := resolution(m)) is not None:
                    if r.settled_at < since:
                        return
                    yield r
            if len(markets) < self.api.limit:
                return
            offset += self.api.limit
            self.api.sleep(self.api.gap)
