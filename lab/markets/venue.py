"""The one adapter interface every venue implements, and the records it returns.

A venue is read-only here: it lists its open markets with their top of book, and the markets that resolved
since a time. Prices are dollars per contract (0 to 1), sizes are contracts. An adapter returns plain records
and never touches the database or object store; lab/markets/recorder.py does that.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True, slots=True)
class Quote:
    """One outcome of one market as seen at `ts`, with the market's metadata alongside."""
    market_id: str
    outcome: str  # "yes" for a binary market; the contract id where a market has several
    ts: dt.datetime
    event_id: str | None
    series_id: str | None
    title: str
    subtitle: str | None
    market_type: str
    status: str
    open_time: dt.datetime | None
    close_time: dt.datetime | None
    bid: float | None
    ask: float | None
    last: float | None
    bid_size: float | None
    ask_size: float | None
    volume: float | None
    open_interest: float | None


@dataclass(frozen=True, slots=True)
class Resolution:
    market_id: str
    result: str  # the venue's own word: "yes", "no", a contract id, or "void"
    value: float | None  # what one yes contract paid, in dollars
    settled_at: dt.datetime


class Venue(Protocol):
    name: str

    def open_markets(self) -> Iterator[list[Quote]]:
        """Every open market, one page at a time, so a 180k-market sweep never sits in memory whole."""
        ...

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        """Markets settled at or after `since`."""
        ...
