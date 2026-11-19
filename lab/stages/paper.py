"""The one paper pass: every market's paper league runs through run_pass, and one ledger, paper_accounts and
paper_orders, holds what it does. A market is an adapter with the hooks below; this module owns the rest: opening and
retiring accounts, the stopped set, the ledger writes, the decision funnel's pruning and each account's mark.

  accounts(conn)                     (account, family, params) of every variant that should trade now
  prepare(conn, now, league)         settle what the market resolved and read the book or quotes; returns ctx
  idle(ctx)                          why nothing trades this pass (a stale book, a closed market), or None
  report(ctx)                        what the pass's result says about ctx
  trade(conn, ctx, account, family, params, capital, entries, now)
                                     one account's pass; `entries` is False for a retired or stopped account that
                                     only exits (when `exits_retired`); returns its counts, or None for no pass
  prices(conn, ctx, held)            {(market_id, side): exit price} for the open orders that have one
  after(conn, ctx, now)              the market's own checks once the account is marked (event contracts' forward)

An order is a paper_orders row: market_id the instrument, side yes, no or long, contracts the size (shares for a
stock), price the fill, fee the entry cost; it closes with settled_at, payout (per unit), exit_fee, exit_reason and
profit net of every cost. Paper only: no hook here can reach an order API.
"""

import datetime as dt
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from lab.decisions import funnel, prune
from lab.grid import variant_of


class Adapter(Protocol):
    venue: str
    league: str            # decisions.league
    capital: float         # each account's paper capital
    trace_days: int        # decisions and funnel rows older than this are cut
    exits_retired: bool    # a retired or stopped account still runs its exits and marks

    def accounts(self, conn) -> list[tuple[str, str, dict]]: ...
    def prepare(self, conn, now: dt.datetime, league: list[tuple[str, str, dict]]) -> Any: ...
    def idle(self, ctx) -> str | None: ...
    def report(self, ctx) -> dict: ...
    def trade(self, conn, ctx, account: str, family: str, params: dict, capital: float, entries: bool,
              now: dt.datetime) -> dict | None: ...
    def prices(self, conn, ctx, held: list[tuple[str, str]]) -> dict[tuple[str, str], float]: ...
    def after(self, conn, ctx, now: dt.datetime) -> dict: ...


def open_accounts(conn, venue: str, league: list[tuple[str, str, dict]], capital: float, now: dt.datetime) -> list[str]:
    """One account per (account, family, params): new ones opened, the venue's others retired (they take no entries;
    their open orders still close and mark), and the ones that joined again revived. Returns the names retired."""
    names = [a for a, _, _ in league]
    conn.cursor().executemany(
        "INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (account) DO NOTHING",
        [(a, f, venue, variant_of(p), Jsonb(p), capital, now) for a, f, p in league])
    taken = conn.execute("SELECT account, venue FROM paper_accounts WHERE account = ANY(%s) AND venue <> %s",
                         (names, venue)).fetchall()
    if taken:
        raise RuntimeError(f"{venue}: account names held by another venue: {taken}")
    retired = [a for (a,) in conn.execute(
        "UPDATE paper_accounts SET retired_at = %s WHERE venue = %s AND retired_at IS NULL "
        "AND NOT account = ANY(%s) RETURNING account", (now, venue, names)).fetchall()]
    conn.execute("UPDATE paper_accounts SET retired_at = NULL WHERE venue = %s AND account = ANY(%s)", (venue, names))
    return retired


def stopped(conn) -> set[str]:
    """Accounts the watcher's drill stopped (lab/watcher.py): they take no entries."""
    return {b for (b,) in conn.execute("SELECT bot FROM watch WHERE stopped_at IS NOT NULL")}


def cash(conn, account: str, capital: float) -> float:
    """Capital plus realized profit less what the open orders cost."""
    realized, held = conn.execute(
        "SELECT coalesce(sum(profit) FILTER (WHERE settled_at IS NOT NULL), 0), "
        "coalesce(sum(contracts * price + fee) FILTER (WHERE settled_at IS NULL AND contracts > 0), 0) "
        "FROM paper_orders WHERE account = %s", (account,)).fetchone()
    return capital + realized - held


def held(conn, account: str) -> int:
    """Open positions: filled orders not yet closed."""
    return conn.execute("SELECT count(*) FROM paper_orders WHERE account = %s AND settled_at IS NULL AND contracts > 0",
                        (account,)).fetchone()[0]


def open_orders(conn, account: str) -> dict[str, dict]:
    """The account's open filled orders by market_id, oldest first."""
    cols = ("id", "market_id", "side", "contracts", "sent_at", "price", "fee")
    return {r[1]: dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM paper_orders WHERE account = %s AND settled_at IS NULL AND contracts > 0 "
        "ORDER BY id", (account,)).fetchall()}


def enter(conn, account: str, market_id: str, side: str, stake: float, limit: float | None, now: dt.datetime,
          quoted_at: dt.datetime, bid: float | None, ask: float | None, contracts: float, price: float | None,
          fee: float) -> int | None:
    """Store one order, filled or not (contracts 0); None when the account already holds one open on the market."""
    row = conn.execute(
        "INSERT INTO paper_orders (account, market_id, side, stake, max_price, sent_at, quoted_at, bid, ask, "
        "contracts, price, fee) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (account, market_id) WHERE settled_at IS NULL DO NOTHING RETURNING id",
        (account, market_id, side, stake, limit, now, quoted_at, bid, ask, contracts, price, fee)).fetchone()
    return row[0] if row else None


def close(conn, closed: list[tuple[dt.datetime, float, float | None, float, str | None, int]]) -> None:
    """Close orders: (at, payout per unit, exit fee, profit net of every cost, reason, order id) each."""
    conn.cursor().executemany(
        "UPDATE paper_orders SET settled_at = %s, payout = %s, exit_fee = %s, profit = %s, exit_reason = %s "
        "WHERE id = %s", closed)


def mark(conn, ad: Adapter, ctx, now: dt.datetime) -> None:
    """Each account's equity: capital, realized profit, and each open order at its exit price (its cost when the
    adapter has none) less its cost."""
    open_: dict[str, list] = {}
    realized: dict[str, float] = {}
    for account, market_id, side, n, price, fee, profit, settled in conn.execute(
            "SELECT o.account, o.market_id, o.side, o.contracts, o.price, o.fee, o.profit, o.settled_at "
            "FROM paper_orders o JOIN paper_accounts a USING (account) WHERE a.venue = %s AND o.contracts > 0",
            (ad.venue,)).fetchall():
        if settled is None:
            open_.setdefault(account, []).append((market_id, side, n, price, fee))
        else:
            realized[account] = realized.get(account, 0.0) + profit
    px = ad.prices(conn, ctx, sorted({(m, s) for rows in open_.values() for m, s, *_ in rows}))
    for account, capital in conn.execute("SELECT account, capital FROM paper_accounts WHERE venue = %s",
                                         (ad.venue,)).fetchall():
        rows = open_.get(account, [])
        unrealized = sum(n * (px.get((m, s), price) - price) - fee for m, s, n, price, fee in rows)
        r = realized.get(account, 0.0)
        conn.execute("UPDATE paper_accounts SET equity = %s, realized = %s, open_positions = %s, marked_at = %s "
                     "WHERE account = %s", (capital + r + unrealized, r, len(rows), now, account))


def run_pass(conn, ad: Adapter, now: dt.datetime) -> dict:
    """One pass of the adapter's league, in one transaction: open and retire accounts, prepare, each account's
    exits and entries, prune the trace, mark, and the adapter's own checks."""
    with conn.transaction():
        league = ad.accounts(conn)
        retired = open_accounts(conn, ad.venue, league, ad.capital, now)
        ctx = ad.prepare(conn, now, league)
        stop = stopped(conn)
        accounts = conn.execute("SELECT account, family, params, capital, retired_at FROM paper_accounts "
                                "WHERE venue = %s ORDER BY account", (ad.venue,)).fetchall()
        out = {"retired": sorted(retired), "stopped": sorted(stop & {a for a, *_ in accounts}),
               "accounts": {}} | ad.report(ctx)
        called = [(a, f, p, c, r is None and a not in stop) for a, f, p, c, r in accounts
                  if ad.exits_retired or (r is None and a not in stop)]
        if (why := ad.idle(ctx)) is not None:
            funnel(conn, ad.league, ad.venue, now, {(a, why): 1 for a, *_ in called})
        else:
            for a, f, p, c, entries in called:
                if (res := ad.trade(conn, ctx, a, f, p, c, entries, now)) is not None:
                    out["accounts"][a] = res
        prune(conn, ad.league, ad.venue, now - dt.timedelta(days=ad.trace_days))
        mark(conn, ad, ctx, now)
        out |= ad.after(conn, ctx, now)
    return out
