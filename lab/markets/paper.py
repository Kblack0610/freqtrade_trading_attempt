#!/usr/bin/env python3
"""The event-contract paper league: strategy families trade paper money against the live recorded books.

  python -m lab.markets.paper [--venue kalshi]     # one pass; the CronJob runs it after each recorder sweep
  python -m lab.markets.paper --venue V --report   # the forward test's weekly ROI and CLV (report), for lab-status

The league follows the readiness gate: every market_readiness row (lab/markets/families.py gate()) on the venue that
is ready, of a family that can run here (FAMILIES), is one account with `[paper] capital`, trading the params the gate
judged. A `[paper] forward` family of the venue joins with every variant of its grid, gate or not: the paper
forward test is the evidence it is waiting on (tt_totals, whose backtest needs history no source sells yet). An account whose variant is no longer ready is retired (retired_at): it takes no entries, its open fills still
settle and mark, and it comes back if the variant passes again. A ready variant of a family that cannot run here is
reported under "unrunnable". A pass:
0. Opens an account for each ready variant and retires the rest.
1. Settles every open fill whose market has a resolution, with backtest.payout and fees.settlement.
2. Shows each account the venue's latest recorded book: the recorder's `outcomes` row of every open yes outcome
   closing within `horizon_h`, as the row backtest.replay would show it; a sportsbook's (kind sportsbook) is its
   line book (line_books, families.line_book), one row per totals line. It does so only when the venue's last good
   sweep is under `max_book_age_min` old, so a dead recorder means no entries and not entries on old prices.
3. Fills each order with backtest.take against that row (crossing the spread, at most `[backtest] depth_share` of
   the side's size) and fees.entry, so a paper fill and a backtest fill of the same book cost the same. Every order
   is stored, filled or not, so an account enters a market once, as in a backtest. An order the account's cash
   cannot pay for, or one past `[paper] max_open` open positions, is dropped unstored and asked again next pass.
4. Records each open fill's closing line so far (closing_line), for its CLV, and marks each account: capital, plus
   realized profit, plus each open position at its exit price (a yes at the bid, a no at one minus the ask) less its
   cost. A sportsbook bet has no exit, so it marks at its cost. A line settles off its game's `Total N`.
5. Checks each account forward (forward): its settled per-trade returns against its variant's out-of-sample
   market_backtests returns, the difference of means bootstrapped (metrics.diff_ci) once `[paper] drift_min_trades`
   have settled; `drifted` when that interval excludes zero.

An account whose `watch` row is stopped (lab/watcher.py --drill) takes no entries; its fills still settle and mark.
Paper only: nothing here can reach a venue's order API, and nothing places a bet at a sportsbook (lab/live.py
holds the guard). Families that need a fit or a key are not in the league yet: calibration (a bars fit), gap (odds_api is not deployed) and llm (no LiteLLM key; forward scoring is its only
scoring, so it joins once the key is in SOPS).
"""

import argparse
import datetime as dt
import math
import sys
from collections import namedtuple

import pandas as pd
from psycopg.types.json import Jsonb

from lab import metrics
from lab.db import connect
from lab.grid import variants
from lab.markets import backtest, fees
from lab.markets.backtest import Market
from lab.markets.families import Longshot, TTTotals, line_book, line_resolution, meetings_of
from lab.markets.recorder import load_config

UTC = dt.timezone.utc
FAMILIES = ("longshot", "tt_totals")
Row = namedtuple("Row", backtest.BOOK)


def account_of(family: str, variant: str) -> str:
    return f"paper-{family}-{variant[:8]}"


def accounts(conn, venue: str, cfg: dict) -> tuple[list[tuple[str, str, dict]], list[str]]:
    """(account, family, params) for every ready variant on the venue of a family that can run here and every variant
    of a `[paper] forward` family of the venue, and the family/variant of every ready one that cannot run here."""
    league, unrunnable = {}, []
    for fam, variant, params in conn.execute(
            "SELECT family, variant, params FROM market_readiness WHERE venue = %s AND ready ORDER BY family, variant",
            (venue,)).fetchall():
        if fam in FAMILIES:
            league[account_of(fam, variant)] = (fam, params)
        else:
            unrunnable.append(f"{fam}/{variant[:8]}")
    for fam in cfg["paper"].get("forward", {}).get(venue, []):
        for params in variants(cfg["families"][fam]):
            league.setdefault(account_of(fam, backtest.variant_of(params)), (fam, params))
    return [(a, f, p) for a, (f, p) in sorted(league.items())], unrunnable


def strategy_of(family: str, params: dict, meetings, fairs: dict):
    """A fresh strategy for one pass; tt_totals reads the venue's meetings and the line book's no-vig fairs."""
    return TTTotals(params, meetings, fairs) if family == "tt_totals" else Longshot(params)


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


def line_books(conn, venue: str, now: dt.datetime, horizon_h: float,
               depth: float) -> tuple[list[tuple[Row, Market]], dict]:
    """A sportsbook's book as it stands now: families.line_book over the recorder's latest quote of every outcome of
    each unresolved totals market closing within horizon_h, one row per line at `now` (the recorder upserts only a
    changed quote, so the newest row per line is the whole board), and the no-vig fair of each line's over."""
    q = pd.DataFrame(conn.execute(
        """SELECT o.market_id, o.quoted_at, o.outcome, o.bid, o.ask
           FROM outcomes o JOIN markets m USING (venue, market_id)
           WHERE o.venue = %s AND m.market_type = 'totals' AND m.status NOT IN ('settled', 'finalized')
             AND m.close_time > %s AND m.close_time <= %s
             AND NOT EXISTS (SELECT 1 FROM resolutions r WHERE r.venue = o.venue AND r.market_id = o.market_id)""",
        (venue, now, now + dt.timedelta(hours=horizon_h))).fetchall(),
        columns=["market_id", "ts", "outcome", "bid", "ask"], dtype=object)
    if not len(q):
        return [], {}
    q[["bid", "ask"]] = q[["bid", "ask"]].astype(float)
    markets, _ = backtest.load_markets(conn, venue, q["market_id"].unique())
    book, lm, _, fair = line_book(q, markets, {}, depth)
    at, out, fairs = pd.Timestamp(now), [], {}
    for r in book.groupby("market_id", sort=True).tail(1).itertuples(index=False):
        out.append((Row(r.market_id, at, r.bid, r.ask, r.depth_yes, r.depth_no), lm[r.market_id]))
        if (f := fair.get((r.market_id, pd.Timestamp(r.ts).value))) is not None:
            fairs[(r.market_id, at.value)] = f
    return out, fairs


def book_age(conn, venue: str, now: dt.datetime) -> dt.timedelta | None:
    last = conn.execute("SELECT max(finished_at) FROM market_sweeps WHERE venue = %s AND ok", (venue,)).fetchone()[0]
    return None if last is None else now - last


def settle(conn, venue: str, fee_cfg: dict | None) -> int:
    """Every open fill of a resolved market, settled. Returns how many."""
    done = []
    for oid, mid, side, n, price, fee, *res in conn.execute(
            """SELECT p.id, p.market_id, p.side, p.contracts, p.price, p.fee, r.market_id, r.result, r.value,
                      r.settled_at, r.volume
               FROM paper_orders p JOIN paper_accounts a USING (account)
               JOIN resolutions r ON r.venue = a.venue AND r.market_id = split_part(p.market_id, '|', 1)
               WHERE a.venue = %s AND p.settled_at IS NULL AND p.contracts > 0""", (venue,)).fetchall():
        r = backtest.Resolution(*res)
        r = line_resolution(mid, r) if "|" in mid else r
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


def held(conn, account: str) -> int:
    """Open positions: fills not yet settled."""
    return conn.execute("SELECT count(*) FROM paper_orders WHERE account = %s AND settled_at IS NULL AND contracts > 0",
                        (account,)).fetchone()[0]


def trade(conn, account: str, strategy, book: list[tuple[Row, Market]], capital: float, share: float,
          fee_cfg: dict | None, now: dt.datetime, max_open: int) -> dict:
    entered = [m for (m,) in conn.execute("SELECT market_id FROM paper_orders WHERE account = %s", (account,))]
    strategy.seen = set(entered) | {m.split("|")[0] for m in entered}  # a line's game, which tt_totals enters once
    left, sent, filled, broke, capped, open_ = cash(conn, account, capital), 0, 0, 0, 0, held(conn, account)
    taken: dict = {}
    for row, m in book:
        for o in strategy.on_book(row, m) or ():
            got = backtest.take(o, row.bid, row.ask, row.depth_yes, row.depth_no, taken.get((o.market_id, o.side), 0),
                                share)
            n, price = got or (0, None)
            fee = fees.entry(fee_cfg, n, price) if n else 0.0
            if n and open_ >= max_open:
                strategy.seen -= {o.market_id, m.event_id}
                capped += 1
                continue
            if n and n * price + fee > left:
                strategy.seen -= {o.market_id, m.event_id}
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
                open_ += 1
                left -= n * price + fee
                taken[(o.market_id, o.side)] = taken.get((o.market_id, o.side), 0) + n
    return {"orders": sent, "fills": filled, "no_cash": broke, "capped": capped}


def closing_line(conn, venue: str, book: list[tuple[Row, Market]], now: dt.datetime) -> None:
    """Each open fill's closing line so far: the yes mid of its market's row in this pass's book. The book holds only
    markets not yet closed, so the last pass before the close leaves the closing line, as of the venue's last sweep
    before it (backtest.clv's closing price, on the paper book)."""
    mids = [((r.bid + r.ask) / 2, now, venue, r.market_id) for r, _ in book
            if math.isfinite(r.bid) and math.isfinite(r.ask)]
    conn.cursor().executemany(
        "UPDATE paper_orders p SET close_mid = %s, close_at = %s FROM paper_accounts a WHERE a.account = p.account "
        "AND a.venue = %s AND p.market_id = %s AND p.settled_at IS NULL AND p.contracts > 0", mids)


def forward(conn, venue: str, min_trades: int, now: dt.datetime) -> int:
    """Each account's settled per-trade returns (profit over cost with fees, backtest's profit_ratio) against its
    variant's market_backtests returns on the venue. Writes the check on paper_accounts; returns how many drifted."""
    drifted = 0
    for account, family, variant in conn.execute(
            "SELECT account, family, variant FROM paper_accounts WHERE venue = %s ORDER BY account", (venue,)).fetchall():
        paid = [float(r) for (r,) in conn.execute(
            "SELECT profit / nullif(contracts * price + fee, 0) FROM paper_orders "
            "WHERE account = %s AND settled_at IS NOT NULL AND contracts > 0", (account,)).fetchall() if r is not None]
        bt = [x for (rs,) in conn.execute(
            "SELECT returns FROM market_backtests WHERE family = %s AND venue = %s AND variant = %s",
            (family, venue, variant)).fetchall() for x in rs]
        lo, hi = (metrics.diff_ci(paid, bt, metrics.seed_of(account, "forward"))
                  if len(paid) >= min_trades and bt else (None, None))
        drift = None if lo is None else bool(lo > 0 or hi < 0)
        drifted += bool(drift)
        conn.execute(
            "UPDATE paper_accounts SET paper_trades = %s, paper_expectancy = %s, backtest_trades = %s, "
            "backtest_expectancy = %s, gap_lo = %s, gap_hi = %s, drifted = %s, checked_at = %s WHERE account = %s",
            (len(paid), float(sum(paid) / len(paid)) if paid else None, len(bt),
             float(sum(bt) / len(bt)) if bt else None, lo, hi, drift, now, account))
    return drifted


def run_pass(conn, venue: str, cfg: dict, now: dt.datetime) -> dict:
    pcfg, bcfg = cfg["paper"], cfg["backtest"]
    fee_cfg = bcfg.get("fees", {}).get(venue)
    sportsbook = cfg["venues"][venue].get("kind") == "sportsbook"
    with conn.transaction():
        league, unrunnable = accounts(conn, venue, cfg)
        names = [a for a, _, _ in league]
        conn.cursor().executemany(
            "INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (account) DO NOTHING",
            [(a, f, venue, backtest.variant_of(p), Jsonb(p), pcfg["capital"], now) for a, f, p in league])
        retired = [a for (a,) in conn.execute(
            "UPDATE paper_accounts SET retired_at = %s WHERE venue = %s AND retired_at IS NULL "
            "AND NOT account = ANY(%s) RETURNING account", (now, venue, names)).fetchall()]
        conn.execute("UPDATE paper_accounts SET retired_at = NULL WHERE account = ANY(%s)", (names,))
        settled = settle(conn, venue, fee_cfg)
        age = book_age(conn, venue, now)
        fresh = age is not None and age <= dt.timedelta(minutes=pcfg["max_book_age_min"])
        book, fairs = ((line_books(conn, venue, now, pcfg["horizon_h"], bcfg["sportsbook_depth"]) if sportsbook
                        else (books(conn, venue, now, pcfg["horizon_h"]), {})) if fresh else ([], {}))
        meetings = meetings_of(conn, venue) if any(f == "tt_totals" for _, f, _ in league) else None
        stopped = {b for (b,) in conn.execute("SELECT bot FROM watch WHERE stopped_at IS NOT NULL")}
        capital = dict(conn.execute("SELECT account, capital FROM paper_accounts WHERE venue = %s", (venue,)).fetchall())
        out = {"settled": settled, "book": len(book), "fresh": fresh, "stopped": sorted(stopped & capital.keys()),
               "retired": sorted(retired), "unrunnable": unrunnable, "accounts": {}}
        for a, f, p in league:
            if a not in stopped:
                out["accounts"][a] = trade(conn, a, strategy_of(f, p, meetings, fairs), book, capital[a],
                                           bcfg["depth_share"], fee_cfg, now, pcfg["max_open"])
        closing_line(conn, venue, book, now)
        mark(conn, venue, now)
        out["drifted"] = forward(conn, venue, pcfg["drift_min_trades"], now)
    return out


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def report(conn, venue: str, pcfg: dict, now: dt.datetime) -> list[str]:
    """The league's forward test on the venue, as lines for the weekly lab-status: per family, the first fill, the
    settled bets and when the test is done (`forward_days` since the first fill and `forward_bets` settled, the later
    of the two); per account and ISO week of settlement, bets, W-L, ROI (profit over cost, fees in) and CLV (the
    closing line of the side bought less its price, over the fills that have one)."""
    rows = conn.execute(
        """SELECT a.family, a.account, p.sent_at, p.settled_at, p.side, p.contracts * p.price + p.fee, p.profit,
                  p.price, p.close_mid
           FROM paper_orders p JOIN paper_accounts a USING (account)
           WHERE a.venue = %s AND p.contracts > 0 ORDER BY a.family, a.account, p.settled_at""", (venue,)).fetchall()
    if not rows:
        return [f"{venue}: no paper fills yet"]
    out, days, need = [], pcfg["forward_days"], pcfg["forward_bets"]
    for fam in sorted({r[0] for r in rows}):
        fr = [r for r in rows if r[0] == fam]
        first, done = min(r[2] for r in fr), [r for r in fr if r[3] is not None]
        ends = first + dt.timedelta(days=days)
        state = "done" if now >= ends and len(done) >= need else f"runs to {ends:%Y-%m-%d} and {need} settled bets"
        out.append(f"{fam} on {venue}: {len({r[1] for r in fr})} accounts, first fill {first:%Y-%m-%d} "
                   f"(day {(now - first).days}), {len(done)} settled; forward test {state}")
        weeks: dict = {}
        for r in done:
            y, w, _ = r[3].isocalendar()
            weeks.setdefault((f"{y}-W{w:02d}", r[1]), []).append(r)
        for (week, account), wr in sorted(weeks.items()):
            cost, profit = sum(r[5] for r in wr), sum(r[6] for r in wr)
            clv = [(r[8] if r[4] == "yes" else 1 - r[8]) - r[7] for r in wr if r[8] is not None]
            line = (f"  {week} {account}: {len(wr)} bets, {sum(r[6] > 0 for r in wr)}-{sum(r[6] < 0 for r in wr)}, "
                    f"ROI {_pct(profit / cost) if cost else 'n/a'}")
            out.append(line + (f", CLV {sum(clv) / len(clv):+.3f} over {len(clv)}" if clv else ", CLV n/a"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="markets.paper")
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--report", action="store_true", help="print the forward test's weekly ROI and CLV, trade nothing")
    a = ap.parse_args(argv)
    cfg = load_config()
    if a.venue not in cfg["paper"]["venues"]:
        sys.exit(f"paper: {a.venue} is not a paper venue ({cfg['paper']['venues']})")
    with connect(autocommit=True) as conn:
        if a.report:
            print("\n".join(report(conn, a.venue, cfg["paper"], dt.datetime.now(UTC))), flush=True)
            return 0
        out = run_pass(conn, a.venue, cfg, dt.datetime.now(UTC))
    if not out["fresh"]:
        print(f"paper: {a.venue}: no recent good sweep, so no entries this pass", file=sys.stderr, flush=True)
    print(f"paper: {a.venue}: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
