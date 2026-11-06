#!/usr/bin/env python3
"""The event-contract paper league: strategy families trade paper money against the live recorded books.

  python -m lab.markets.paper [--venue kalshi]     # one pass; the CronJob runs it after each recorder sweep

Each variant of each family in `[paper] families` (lab/markets.toml) is one account with `[paper] capital`. A pass:
1. Settles every open fill whose market has a resolution, with backtest.payout and fees.settlement.
2. Shows each account the venue's latest recorded book: the recorder's `outcomes` row of every open yes outcome
   closing within `horizon_h`, as the row backtest.replay would show it. It does so only when the venue's last good
   sweep is under `max_book_age_min` old, so a dead recorder means no entries and not entries on old prices.
3. Fills each order with backtest.take against that row (crossing the spread, at most `[backtest] depth_share` of
   the side's size) and fees.entry, so a paper fill and a backtest fill of the same book cost the same. Every order
   is stored, filled or not, so an account enters a market once, as in a backtest. An order the account's cash
   cannot pay for is dropped unstored and asked again next pass.
4. Marks each account: capital, plus realized profit, plus each open position at its exit price (a yes at the bid,
   a no at one minus the ask) less its cost.

An account whose `watch` row is stopped (lab/watcher.py --drill) takes no entries; its fills still settle and mark.
Paper only: nothing here can reach a venue's order API. Families that need a fit or a key are not in the league
yet: calibration (a bars fit), gap (odds_api is not deployed) and llm (no LiteLLM key; forward scoring is its only
scoring, so it joins once the key is in SOPS).
"""

import argparse
import datetime as dt
import math
import sys
from collections import namedtuple

import pandas as pd
from psycopg.types.json import Jsonb

from lab.db import connect
from lab.markets import backtest, fees
from lab.markets.backtest import Market
from lab.markets.families import Longshot, variants
from lab.markets.recorder import load_config

UTC = dt.timezone.utc
FAMILIES = {"longshot": Longshot}
Row = namedtuple("Row", backtest.BOOK)


def account_of(family: str, params: dict) -> str:
    return f"paper-{family}-{backtest.variant_of(params)[:8]}"


def accounts(cfg: dict) -> list[tuple[str, object]]:
    """(account, a fresh strategy) for every variant of every league family."""
    out = []
    for fam in cfg["paper"]["families"]:
        if fam not in FAMILIES:
            raise ValueError(f"family {fam!r} cannot run in the paper league (module docstring)")
        out += [(account_of(fam, p), FAMILIES[fam](p)) for p in variants(cfg["families"][fam])]
    return out


def _f(x) -> float:
    return math.nan if x is None else float(x)


def books(conn, venue: str, now: dt.datetime, horizon_h: float) -> list[tuple[Row, Market]]:
    """The latest recorded book of every unresolved open yes outcome closing within horizon_h, oldest quote first."""
    rows = conn.execute(
        """SELECT o.market_id, o.quoted_at, o.bid, o.ask, o.ask_size, o.bid_size, m.event_id, m.series_id, m.title,
                  m.subtitle, m.market_type, m.open_time, m.close_time
           FROM outcomes o JOIN markets m USING (venue, market_id)
           WHERE o.venue = %s AND o.outcome = 'yes' AND m.status NOT IN ('settled', 'finalized')
             AND m.close_time > %s AND m.close_time <= %s
             AND NOT EXISTS (SELECT 1 FROM resolutions r WHERE r.venue = o.venue AND r.market_id = o.market_id)
           ORDER BY o.quoted_at, o.market_id""",
        (venue, now, now + dt.timedelta(hours=horizon_h))).fetchall()
    return [(Row(r[0], pd.Timestamp(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5])), Market(r[0], *r[6:]))
            for r in rows]


def book_age(conn, venue: str, now: dt.datetime) -> dt.timedelta | None:
    last = conn.execute("SELECT max(finished_at) FROM market_sweeps WHERE venue = %s AND ok", (venue,)).fetchone()[0]
    return None if last is None else now - last


def settle(conn, venue: str, fee_cfg: dict | None) -> int:
    """Every open fill of a resolved market, settled. Returns how many."""
    done = []
    for oid, side, n, price, fee, *res in conn.execute(
            """SELECT p.id, p.side, p.contracts, p.price, p.fee, r.market_id, r.result, r.value, r.settled_at, r.volume
               FROM paper_orders p JOIN paper_accounts a USING (account)
               JOIN resolutions r ON r.venue = a.venue AND r.market_id = p.market_id
               WHERE a.venue = %s AND p.settled_at IS NULL AND p.contracts > 0""", (venue,)).fetchall():
        r = backtest.Resolution(*res)
        if (pay := backtest.payout(side, price, r)) is None:
            continue
        profit = n * pay - (n * price + fee) - fees.settlement(fee_cfg, n * price, n * pay)
        done.append((r.settled_at, pay, profit, oid))
    with conn.cursor() as cur:
        cur.executemany("UPDATE paper_orders SET settled_at = %s, payout = %s, profit = %s WHERE id = %s", done)
    return len(done)


def mark(conn, venue: str, now: dt.datetime) -> None:
    """Each account's equity: capital, realized profit, and open fills at their exit price less their cost."""
    conn.execute(
        """UPDATE paper_accounts a SET equity = a.capital + s.realized + s.unrealized, realized = s.realized,
               open_positions = s.open, marked_at = %s
           FROM (SELECT a.account,
                        coalesce(sum(p.profit) FILTER (WHERE p.settled_at IS NOT NULL), 0) AS realized,
                        coalesce(sum(p.contracts * coalesce(
                            CASE WHEN p.side = 'yes' AND o.bid > 0 AND o.bid < 1 THEN o.bid
                                 WHEN p.side = 'no' AND o.ask > 0 AND o.ask < 1 THEN 1 - o.ask END, p.price)
                            - p.contracts * p.price - p.fee)
                          FILTER (WHERE p.settled_at IS NULL AND p.contracts > 0), 0) AS unrealized,
                        count(*) FILTER (WHERE p.settled_at IS NULL AND p.contracts > 0) AS open
                 FROM paper_accounts a LEFT JOIN paper_orders p USING (account)
                 LEFT JOIN outcomes o ON o.venue = a.venue AND o.market_id = p.market_id AND o.outcome = 'yes'
                 WHERE a.venue = %s GROUP BY a.account) s
           WHERE a.account = s.account""", (now, venue))


def cash(conn, account: str, capital: float) -> float:
    """Capital plus realized profit less what the open fills cost."""
    realized, held = conn.execute(
        "SELECT coalesce(sum(profit) FILTER (WHERE settled_at IS NOT NULL), 0), "
        "coalesce(sum(contracts * price + fee) FILTER (WHERE settled_at IS NULL AND contracts > 0), 0) "
        "FROM paper_orders WHERE account = %s", (account,)).fetchone()
    return capital + realized - held


def trade(conn, account: str, strategy, book: list[tuple[Row, Market]], capital: float, share: float,
          fee_cfg: dict | None, now: dt.datetime) -> dict:
    strategy.seen = {m for (m,) in conn.execute("SELECT market_id FROM paper_orders WHERE account = %s", (account,))}
    left, sent, filled, broke = cash(conn, account, capital), 0, 0, 0
    taken: dict = {}
    for row, m in book:
        for o in strategy.on_book(row, m) or ():
            got = backtest.take(o, row.bid, row.ask, row.depth_yes, row.depth_no, taken.get((o.market_id, o.side), 0),
                                share)
            n, price = got or (0, None)
            fee = fees.entry(fee_cfg, n, price) if n else 0.0
            if n and n * price + fee > left:
                strategy.seen.discard(o.market_id)
                broke += 1
                continue
            conn.execute(
                """INSERT INTO paper_orders (account, market_id, side, stake, max_price, sent_at, quoted_at, bid, ask,
                       contracts, price, fee) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account, market_id) DO NOTHING""",
                (account, o.market_id, o.side, o.stake, o.limit, now, row.ts.to_pydatetime(),
                 None if math.isnan(row.bid) else row.bid, None if math.isnan(row.ask) else row.ask, n, price, fee))
            sent += 1
            if n:
                filled += 1
                left -= n * price + fee
                taken[(o.market_id, o.side)] = taken.get((o.market_id, o.side), 0) + n
    return {"orders": sent, "fills": filled, "no_cash": broke}


def run_pass(conn, venue: str, cfg: dict, now: dt.datetime) -> dict:
    pcfg, bcfg = cfg["paper"], cfg["backtest"]
    fee_cfg = bcfg.get("fees", {}).get(venue)
    league = accounts(cfg)
    with conn.transaction():
        conn.cursor().executemany(
            "INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (account) DO NOTHING",
            [(a, s.name, venue, backtest.variant_of(s.params), Jsonb(s.params), pcfg["capital"], now)
             for a, s in league])
        settled = settle(conn, venue, fee_cfg)
        age = book_age(conn, venue, now)
        fresh = age is not None and age <= dt.timedelta(minutes=pcfg["max_book_age_min"])
        book = books(conn, venue, now, pcfg["horizon_h"]) if fresh else []
        stopped = {b for (b,) in conn.execute("SELECT bot FROM watch WHERE stopped_at IS NOT NULL")}
        capital = dict(conn.execute("SELECT account, capital FROM paper_accounts WHERE venue = %s", (venue,)).fetchall())
        out = {"settled": settled, "book": len(book), "fresh": fresh, "stopped": sorted(stopped & capital.keys()),
               "accounts": {}}
        for a, s in league:
            if a not in stopped:
                out["accounts"][a] = trade(conn, a, s, book, capital[a], bcfg["depth_share"], fee_cfg, now)
        mark(conn, venue, now)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="markets.paper")
    ap.add_argument("--venue", default="kalshi")
    a = ap.parse_args(argv)
    cfg = load_config()
    if a.venue not in cfg["paper"]["venues"]:
        sys.exit(f"paper: {a.venue} is not a paper venue ({cfg['paper']['venues']})")
    with connect(autocommit=True) as conn:
        out = run_pass(conn, a.venue, cfg, dt.datetime.now(UTC))
    if not out["fresh"]:
        print(f"paper: {a.venue}: no recent good sweep, so no entries this pass", file=sys.stderr, flush=True)
    print(f"paper: {a.venue}: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
