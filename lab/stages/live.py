"""The one live door for an order client: any Orders adapter (lab/market.py), whatever its market, places a real-money
order only through place() here, and only when lab/guard.py refusals() names no reason. GUARD_LIFTED is False, so
every place() is refused before the client is asked to place. lab/live.py is the Freqtrade engine's door onto the same
refusals(), and lab/league.py names them when it refuses a config. Nothing deploys a live order path. See docs/live.md.

The guard reads, per order: the stake (price x count) against the [live] stage's cap, the open positions plus this one
against its max_open, the client's venue against [live] venues, the strategy at ready on that venue (lab/readiness.py)
and the bankroll kill switch (lab/watcher.py).
"""

from lab import guard, readiness, suggest
from lab.market import Orders


class Refused(Exception):
    """A real-money order the guard refused; the message names every reason."""


def refusals(client: Orders, conn, market: str, strategy: str, stake: float, config: dict | None = None) -> list[str]:
    """Why this order may not go out, empty when it may. Reads the client's positions and the database, never writes."""
    config = config or suggest.load_config()
    live = config["live"]
    slots = sum(1 for p in client.positions() if p.count) + 1
    return guard.refusals(client.venue, stake, slots, live, guard.stage_of(live, config["book"]),
                          readiness.at_ready(conn, market, client.venue, strategy), guard.killed(conn))


def place(client: Orders, conn, market: str, strategy: str, ticker: str, price: float, count: int, client_id: str,
          config: dict | None = None) -> dict:
    """One limit buy through the door; raises Refused before the client sees it when the guard names a reason."""
    problems = refusals(client, conn, market, strategy, price * count, config)
    if problems:
        raise Refused("; ".join(problems))
    return client.place(ticker, price, count, client_id)
