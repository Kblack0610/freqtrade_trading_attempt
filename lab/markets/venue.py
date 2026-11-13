"""The adapter interfaces every venue implements, and the records they return.

A Venue is read-only: it lists its open markets with their top of book, and the markets that resolved
since a time. A venue with history also serves it (the History methods), which lab/markets/history.py backfills. Prices are dollars per contract (0 to 1), sizes are contracts. An adapter returns plain records
and never touches the database or object store; lab/markets/recorder.py does that.

An order client (Orders) is the one order path, whatever the venue or asset: lab/broker.py AlpacaPaper,
lab/markets/kalshi_trade.py KalshiDemo and lab/markets/manifold_trade.py ManifoldBets implement it, lab/markets/drill.py
drives it, and each one refuses every host but its mock-funds one.
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


@dataclass(frozen=True, slots=True)
class Position:
    """What the account holds of one instrument: count is signed, negative for a short or a no side."""
    ticker: str
    count: float
    cost: float | None  # what the holding cost, in the account's currency, where the venue states it


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    ticker: str
    ts: dt.datetime
    count: float
    price: float  # per unit (a contract's yes price, or a share's)


class Orders(Protocol):
    """An order client. resting: the statuses that mean an order is on the book; terminal: the ones a cancel ends in."""
    venue: str
    resting: frozenset[str]
    terminal: frozenset[str]

    def place(self, ticker: str, price: float, count: int, client_id: str) -> dict:
        """One limit buy; returns {"order_id": ...}. Never retried by a caller: a timeout could have placed it."""
        ...

    def order(self, order_id: str) -> dict:
        """The order as the venue reads it now, with at least its "status"."""
        ...

    def cancel(self, order_id: str, ticker: str) -> object:
        """Returns once the order reads terminal, or after the client's bound (lab/markets/drill.py await_terminal)."""
        ...

    def positions(self) -> list[Position]:
        ...

    def fills(self) -> list[Fill]:
        """The account's most recent fills, newest first, one page."""
        ...
