"""The one adapter interface every venue implements, and the records it returns.

A venue is read-only here: it lists its open markets with their top of book, and the markets that resolved
since a time. A venue with history also serves it (the History methods), which lab/markets/history.py backfills. Prices are dollars per contract (0 to 1), sizes are contracts. An adapter returns plain records
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
    volume: float | None = None  # contracts traded over the market's life, where the venue states it


@dataclass(frozen=True, slots=True)
class Settled:
    """One settled market from a history walk: what it was (`market`, its book at settlement) and how it paid."""
    market: Quote
    resolution: Resolution


@dataclass(frozen=True, slots=True)
class Bar:
    """One period of one market's yes side, ending at `ts`. bid and ask close the period; open to close are trades."""
    market_id: str
    ts: dt.datetime
    period_s: int
    bid: float | None
    ask: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    open_interest: float | None


@dataclass(frozen=True, slots=True)
class Trade:
    market_id: str
    trade_id: str
    ts: dt.datetime
    price: float  # the yes price, dollars
    count: float
    taker_side: str | None  # yes or no


@dataclass(frozen=True, slots=True)
class Span:
    """A settled market as the per-market history calls need it."""
    market_id: str
    series_id: str | None
    open_time: dt.datetime | None
    settled_at: dt.datetime


class Venue(Protocol):
    name: str

    def open_markets(self) -> Iterator[list[Quote]]:
        """Every open market, one page at a time, so a 180k-market sweep never sits in memory whole."""
        ...

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        """Markets settled at or after `since`."""
        ...


class History(Protocol):
    """What a venue serves of its past. A venue implements the methods its API has; history.py asks for no others."""
    name: str

    def settled(self, cursor: str | None) -> Iterator[tuple[list[Settled], str | None]]:
        """Every settled market, newest first, a page at a time with the cursor that resumes after that page
        (None once the walk is over). A cursor from an earlier run starts where that run stopped."""
        ...

    def bars(self, span: Span, period_s: int) -> list[Bar]:
        ...

    def trades(self, span: Span) -> Iterator[list[Trade]]:
        ...
