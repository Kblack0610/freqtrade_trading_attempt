"""The one market contract: the adapter interfaces every market implements, the records they return, and the cost
and fill models every stage shares, so a backtest fill and a paper fill of the same order cost the same.

A Venue is read-only: it lists its open markets with their top of book, and the markets that resolved since a time.
A venue with history also serves it (the History methods), which lab/events/history.py backfills. Prices are
dollars per unit: per contract (0 to 1) on an event contract, per share on a stock; sizes are contracts or shares.
An adapter returns plain records and never touches the database or object store; its stage does that
(lab/stages/recorder.py for event contracts). Adapters: lab/events/ for event contracts and sportsbooks (ADAPTERS
in lab/stages/recorder.py), lab/stocks/paper.py AlpacaStocks for stocks.

An order client (Orders) is the one order path, whatever the venue or asset: lab/money/broker.py AlpacaPaper,
lab/events/kalshi_trade.py KalshiDemo and lab/events/manifold_trade.py ManifoldBets implement it, lab/stages/drill.py
drives it, and each one refuses every host but its mock-funds one.

Cost model (docs/markets.md, each venue's Fees). An event-contract venue's fee table is `[backtest.fees.<venue>]` in
lab/events/markets.toml, one of two kinds:
- `pq`: a taker fee per order of `rate * C * P * (1 - P)` dollars, rounded per order as the venue states it:
  `ceil` up to the cent (Kalshi), `half_even` to the cent (Polymarket US), or `none`. Polymarket global's rate
  depends on the market's category, and the markets table holds none, so its rate is the highest category's.
- `profit`: a share of the profit on a winning settlement and nothing on entry (PredictIt).
A venue with no table (Manifold, play money) costs nothing. A stock pays lab/inputs/calls.toml `cost_bps` of its notional on
each side (bps_fee): Alpaca charges no commission, so it stands for the half spread plus slippage.

Fill model (take, payout). A taker order crosses the spread: YES at the ask, NO at one minus the bid. It takes whole
contracts, at most its stake and its limit price allow, and at most `share` of that side's depth less what earlier
orders took from the same book row; the rest is cancelled. A fill is held to resolution: YES pays the resolution's
value, NO pays one minus it, and a void market refunds the price but not the fee. A stock buys at the ask and sells
at the bid, in fractional shares, with no depth limit (lab/stocks/paper.py).
"""

import datetime as dt
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Iterator, Protocol

SIDES = ("yes", "no")
CENT = Decimal("0.01")
ROUNDING = {"ceil": ROUND_CEILING, "half_even": ROUND_HALF_EVEN}


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

    def resolutions(self, since: dt.datetime, cursor: str | None = None) -> Iterator[tuple[list[Resolution], str | None]]:
        """Markets settled at or after `since`, a page at a time with the cursor that resumes after that page (None
        once the walk reaches `since`). A cursor from an earlier walk with the same `since` continues that walk."""
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
        """Returns once the order reads terminal, or after the client's bound (lab/stages/drill.py await_terminal)."""
        ...

    def positions(self) -> list[Position]:
        ...

    def fills(self) -> list[Fill]:
        """The account's most recent fills, newest first, one page."""
        ...


@dataclass(frozen=True, slots=True)
class Order:
    """What a strategy sends: a taker buy of one side of one market."""
    market_id: str
    side: str  # yes or no
    stake: float  # dollars of contracts, before fees
    limit: float | None = None  # the most it pays per contract


def _cents(x: float, mode: str) -> float:
    if mode == "none":
        return x
    # 12 places first, so 1.7500000000000002 from float arithmetic is 1.75 and not ceiled to 1.76
    return float(Decimal(repr(x)).quantize(Decimal("1e-12")).quantize(CENT, rounding=ROUNDING[mode]))


def entry_fee(fcfg: dict | None, contracts: float, price: float) -> float:
    """The fee in dollars for one taker order of `contracts` at `price` dollars."""
    if not fcfg or fcfg["kind"] != "pq" or contracts <= 0:
        return 0.0
    return _cents(fcfg["rate"] * contracts * price * (1.0 - price), fcfg.get("rounding", "none"))


def settlement_fee(fcfg: dict | None, cost: float, payout: float) -> float:
    """The fee in dollars at settlement on a position that cost `cost` and paid `payout`, before entry fees."""
    if not fcfg or fcfg["kind"] != "profit":
        return 0.0
    return _cents(fcfg["share"] * max(0.0, payout - cost), fcfg.get("rounding", "none"))


def bps_fee(bps: float, notional: float = 1.0) -> float:
    """What one side costs at `bps` basis points of `notional`; with no notional, as a fraction of it."""
    return notional * bps / 1e4


def take(o: Order, bid: float, ask: float, depth_yes: float, depth_no: float, taken: float,
         share: float) -> tuple[int, float] | None:
    """The whole contracts and price a taker order gets from one book row (the fill model), `taken` contracts of its
    side already gone to earlier orders; None when it gets nothing."""
    if o.side not in SIDES:
        raise ValueError(f"order side {o.side!r}")
    price = ask if o.side == "yes" else 1.0 - bid
    if not 0 < price < 1 or (o.limit is not None and price > o.limit):
        return None
    room = (depth_yes if o.side == "yes" else depth_no) * share - taken
    n = min(math.floor(o.stake / price + 1e-9), math.floor(room)) if math.isfinite(room) else 0
    return (n, float(price)) if n >= 1 else None


def payout(side: str, price: float, res: Resolution) -> float | None:
    """What one contract bought at `price` pays at resolution (the fill model); None for an unknown result."""
    if res.result == "void":
        return price
    v = res.value if res.value is not None else {"yes": 1.0, "no": 0.0}.get(res.result)
    if v is None:
        return None
    return v if side == "yes" else 1.0 - v
