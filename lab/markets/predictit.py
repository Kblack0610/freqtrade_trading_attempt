"""PredictIt, read-only, from its one snapshot endpoint, `/api/marketdata/all/` (no key, refreshed about once a minute).

Each contract trades as its own yes/no, so a contract is a market here and its PredictIt market is the event, the
way a Kalshi event holds its markets. Buying yes costs bestBuyYesCost (our ask); selling yes gets bestSellYesCost
(our bid). Closed markets leave the snapshot and the venue states no result or settled time (a closed market's
own page shows only last prices), so resolutions() yields nothing rather than guess a winner from a last trade.
PredictIt dates are Eastern time without an offset.
"""

import datetime as dt
import re
from typing import Iterator
from zoneinfo import ZoneInfo

from lab.markets.api import Api, num
from lab.markets.venue import Quote, Resolution

EASTERN = ZoneInfo("America/New_York")


def _eastern(v) -> dt.datetime | None:
    if v in (None, "", "NA"):
        return None
    try:
        t = dt.datetime.fromisoformat(v)
    except (TypeError, ValueError):
        t = _prose(v)
        if t is None:
            return None
    return t if t.tzinfo else t.replace(tzinfo=EASTERN)


PROSE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))? ([ap])\.?m\.? ET (\w+) (\d{1,2}), (\d{4})", re.I)


def _prose(v) -> dt.datetime | None:
    """PredictIt's '2:59:59 a.m. ET January 1, 2027' form; None for anything unreadable, so one field never kills a sweep."""
    m = PROSE.fullmatch(str(v).strip())
    if not m:
        return None
    h, mi, sec, ap, mon, day, yr = m.groups()
    try:
        return dt.datetime.strptime(f"{mon[:3]} {day} {yr} {int(h) % 12 + (12 if ap.lower() == 'p' else 0)}:{mi}:{sec or 0}", "%b %d %Y %H:%M:%S")
    except ValueError:
        return None


def quotes(market: dict, ts: dt.datetime) -> list[Quote]:
    contracts = market.get("contracts") or []
    return [Quote(
        market_id=str(c["id"]), outcome="yes", ts=ts, event_id=str(market["id"]), series_id=None,
        title=market.get("name") or "", subtitle=c.get("name") if len(contracts) > 1 else None,
        market_type="binary", status=(c.get("status") or "").lower(),
        open_time=None, close_time=_eastern(c.get("dateEnd")),
        bid=num(c.get("bestSellYesCost")), ask=num(c.get("bestBuyYesCost")), last=num(c.get("lastTradePrice")),
        bid_size=None, ask_size=None, volume=None, open_interest=None,
    ) for c in contracts]


class PredictIt:
    name = "predictit"

    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api(self.name, cfg, session, sleep)

    def open_markets(self) -> Iterator[list[Quote]]:
        body = self.api.get("/marketdata/all/")
        ts = dt.datetime.now(dt.timezone.utc)
        yield [q for m in body.get("markets") or [] if m.get("status") == "Open" for q in quotes(m, ts)]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        return iter(())
