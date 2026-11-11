"""Sportsbook odds, read-only, through The Odds API (`/v4`, key in ODDS_API_KEY). We never bet at a sportsbook.

One game is one market, keyed by the API's event id, with the sport key as its series. Its outcomes are each book's
price on each side, `<book>:<team>` (FanDuel's Celtics price is `fanduel:Boston Celtics`), so the recorder diffs
every book on its own. A book quote is a price to buy, never to sell: a decimal price d pays 1 for a stake of 1/d,
so 1/d goes in `ask` and `bid` stays empty. The implied odds keep the book's margin; removing it is a strategy's
choice, not the adapter's. A game's close_time is its commence time.

Credits (docs/markets.md, The Odds API): each sport in `sports` costs 1 credit per region per market on every open
sweep, and resolutions read `/scores` with daysFrom, 2 credits per sport. A finished game resolves to the winning
team's name, or "Draw" on a tied score; that name is the outcome word a market_links row carries for this venue.
The key rides in the query string, so an HTTP error is re-raised without the URL, keeping it out of logs and
market_sweeps.error.
"""

import datetime as dt
import math
import os
from typing import Iterator

import requests

from lab.markets.api import Api, num, when
from lab.markets.venue import Quote, Resolution


def quotes(event: dict, ts: dt.datetime) -> list[Quote]:
    base = dict(
        market_id=event["id"], ts=ts, event_id=None, series_id=event.get("sport_key"),
        title=f"{event['away_team']} at {event['home_team']}", subtitle=event.get("sport_title"),
        market_type="h2h", status="open", open_time=None, close_time=when(event.get("commence_time")),
        bid=None, last=None, bid_size=None, ask_size=None, volume=None, open_interest=None,
    )
    out = []
    for book in event.get("bookmakers") or []:
        for market in book.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes") or []:
                price = num(o.get("price"))
                out.append(Quote(outcome=f"{book['key']}:{o['name']}", ask=1 / price if price else None, **base))
    return out


def resolution(event: dict) -> Resolution | None:
    if not event.get("completed") or not event.get("scores"):
        return None
    score = {s["name"]: num(s.get("score")) for s in event["scores"]}
    home, away = score.get(event["home_team"]), score.get(event["away_team"])
    if home is None or away is None:
        return None
    winner = "Draw" if home == away else event["home_team"] if home > away else event["away_team"]
    settled = when(event.get("last_update")) or when(event.get("commence_time"))
    return Resolution(market_id=event["id"], result=winner, value=1.0, settled_at=settled)


class OddsApi:
    name = "odds_api"

    def __init__(self, cfg: dict, session=None, sleep=None, key: str | None = None):
        self.api = Api(self.name, cfg, session, sleep)
        self.sports = list(cfg["sports"])
        self.regions = cfg.get("regions", "us")
        self.key = key or os.environ.get("ODDS_API_KEY")
        if not self.key:
            raise RuntimeError("odds_api: ODDS_API_KEY is not set")

    def _get(self, path: str, params: dict):
        try:
            return self.api.get(path, {"apiKey": self.key, **params})
        except requests.HTTPError as e:
            raise RuntimeError(f"odds_api: HTTP {e.response.status_code} on {path}") from None

    def open_markets(self) -> Iterator[list[Quote]]:
        for sport in self.sports:
            events = self._get(f"/v4/sports/{sport}/odds", {"regions": self.regions, "markets": "h2h",
                                                             "oddsFormat": "decimal", "dateFormat": "iso"})
            ts = dt.datetime.now(dt.timezone.utc)
            yield [q for e in events for q in quotes(e, ts)]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        days = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 86400
        days_from = min(3, max(1, math.ceil(days)))  # the API takes 1 to 3
        for sport in self.sports:
            for e in self._get(f"/v4/sports/{sport}/scores", {"daysFrom": days_from, "dateFormat": "iso"}):
                if (r := resolution(e)) is not None:  # every finished game in the window; storing one is idempotent
                    yield r
