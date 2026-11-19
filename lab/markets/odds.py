"""Sportsbook odds, read-only, through The Odds API (`/v4`, key in ODDS_API_KEY). We never bet at a sportsbook.

One game and one market key (`markets` in lab/markets.toml [venues.odds_api]: h2h, totals, spreads) is one market,
with the sport key as its series and the key as its market_type. The h2h market is keyed by the API's event id, the
others by `<event id>:<key>`. Its outcomes are each book's price on each side, `<book>:<name>`, so the recorder diffs
every book on its own: h2h names the team (`fanduel:Boston Celtics`), totals the side and line (`fanduel:Over 216.5`),
spreads the team and its handicap (`fanduel:Boston Celtics -4.5`); books quote different lines, so each line is its own outcome. A book quote is a price to buy, never to sell: a decimal price d pays 1 for a stake of 1/d,
so 1/d goes in `ask` and `bid` stays empty. The implied odds keep the book's margin; removing it is a strategy's
choice, not the adapter's. A game's close_time is its commence time.

Credits (docs/markets.md, The Odds API): each sport in `sports` costs 1 credit per region per market on every open
sweep, and resolutions read `/scores` with daysFrom, 2 credits per sport. A finished game resolves each of its markets
from the final score (lab/markets/links.py pays reads them): h2h to the winning team's name, or "Draw" on a tied
score, which is the outcome word a market_links row carries; totals to the total points (`Total 216`); spreads to the
home team's margin (`Phoenix Suns -8`).
The key rides in the query string, so an HTTP error is re-raised without the URL, keeping it out of logs and
market_sweeps.error.
"""

import datetime as dt
import math
import os
from typing import Iterator

import requests

from lab.markets.api import Api, num, when
from lab.market import Quote, Resolution


def market_id(event_id: str, key: str) -> str:
    return event_id if key == "h2h" else f"{event_id}:{key}"


def side(o: dict, key: str) -> str:
    """The outcome word of one book price: the name, plus the line for totals and spreads."""
    point = num(o.get("point"))
    if key == "totals" and point is not None:
        return f"{o['name']} {point:g}"
    if key == "spreads" and point is not None:
        return f"{o['name']} {point:+g}"
    return o["name"]


def quotes(event: dict, ts: dt.datetime, markets=("h2h",)) -> list[Quote]:
    base = dict(
        ts=ts, event_id=None, series_id=event.get("sport_key"),
        title=f"{event['away_team']} at {event['home_team']}", subtitle=event.get("sport_title"),
        status="open", open_time=None, close_time=when(event.get("commence_time")),
        bid=None, last=None, bid_size=None, ask_size=None, volume=None, open_interest=None,
    )
    out = []
    for book in event.get("bookmakers") or []:
        for market in book.get("markets") or []:
            if (key := market.get("key")) not in markets:
                continue
            for o in market.get("outcomes") or []:
                price = num(o.get("price"))
                out.append(Quote(market_id=market_id(event["id"], key), market_type=key,
                                 outcome=f"{book['key']}:{side(o, key)}", ask=1 / price if price else None, **base))
    return out


def resolutions_of(event: dict, markets=("h2h",)) -> list[Resolution]:
    """Each of a finished game's markets, resolved from its final score (module docstring)."""
    if not event.get("completed") or not event.get("scores"):
        return []
    score = {s["name"]: num(s.get("score")) for s in event["scores"]}
    h, a = event["home_team"], event["away_team"]
    home, away = score.get(h), score.get(a)
    if home is None or away is None:
        return []
    result = {"h2h": "Draw" if home == away else h if home > away else a,
              "totals": f"Total {home + away:g}", "spreads": f"{h} {home - away:+g}"}
    settled = when(event.get("last_update")) or when(event.get("commence_time"))
    return [Resolution(market_id=market_id(event["id"], k), result=result[k], value=1.0, settled_at=settled)
            for k in markets if k in result]


class OddsApi:
    name = "odds_api"

    def __init__(self, cfg: dict, session=None, sleep=None, key: str | None = None):
        self.api = Api(self.name, cfg, session, sleep)
        self.sports = list(cfg["sports"])
        self.regions = cfg.get("regions", "us")
        self.markets = tuple(cfg.get("markets", ("h2h",)))
        self.credits = 0
        self.key = key or os.environ.get("ODDS_API_KEY")
        if not self.key:
            raise RuntimeError("odds_api: ODDS_API_KEY is not set")

    def spend(self, credits: int, sport: str) -> None:
        """Count the credits a call cost (module docstring) and print the run's total so far, for the monthly limit."""
        self.credits += credits
        print(f"odds_api: {sport} cost {credits} credits, {self.credits} this run", flush=True)

    def _get(self, path: str, params: dict):
        try:
            return self.api.get(path, {"apiKey": self.key, **params})
        except requests.HTTPError as e:
            raise RuntimeError(f"odds_api: HTTP {e.response.status_code} on {path}") from None

    def open_markets(self) -> Iterator[list[Quote]]:
        for sport in self.sports:
            events = self._get(f"/v4/sports/{sport}/odds", {"regions": self.regions, "markets": ",".join(self.markets),
                                                             "oddsFormat": "decimal", "dateFormat": "iso"})
            self.spend(len(self.markets) * len(self.regions.split(",")), sport)
            ts = dt.datetime.now(dt.timezone.utc)
            yield [q for e in events for q in quotes(e, ts, self.markets)]

    def resolutions(self, since: dt.datetime, cursor: str | None = None) -> Iterator[tuple[list[Resolution], str | None]]:
        """A page a sport; the cursor is the index of the next sport."""
        days = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 86400
        days_from = min(3, max(1, math.ceil(days)))  # the API takes 1 to 3
        for i in range(int(cursor or 0), len(self.sports)):
            sport = self.sports[i]
            scores = self._get(f"/v4/sports/{sport}/scores", {"daysFrom": days_from, "dateFormat": "iso"})
            self.spend(2, sport)
            # every finished game in the window; storing one is idempotent
            yield [r for e in scores for r in resolutions_of(e, self.markets)], str(i + 1) if i + 1 < len(self.sports) else None
