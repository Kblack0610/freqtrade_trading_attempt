#!/usr/bin/env python3
"""The stock league: paper accounts trading a small ETF universe against Alpaca's live quotes with simulated fills.

  python -m lab.stocks     # one pass; the CronJob runs it every 5 minutes

Every family variant of lab/stocks.toml is one account with `capital`; a variant no longer in the toml is retired
(retired_at): it takes no entries and its open positions still exit and mark. A pass:
1. Reads each ticker's final daily closes (lab/calls.py final: a bar counts settle_hours after the 16:00 ET close)
   from Alpaca's daily bars, and, inside the session hours, each ticker's latest quote. Outside the session, or when
   no quote is fresher than `max_quote_age_min` (a holiday), the market is closed: each account gets one funnel row
   `market_closed` and nothing trades.
2. Exits first: each open position whose family's exit check passes sells at the bid, when its quote is fresh,
   valid and within `max_spread_bps`.
3. Entries: each ticker of the universe not held, whose family's entry checks and the quote checks all pass, buys
   `stake` of fractional shares at the ask, unless the account holds `max_open` positions or its cash cannot pay.
   Each side pays lab/calls.toml cost_bps on its notional.
4. Marks each account: capital, plus realized profit, plus each open position at the bid (the last close when the
   market is closed) less its cost.
Every entry and exit writes a `stocks` decision in the same transaction (lab/decisions.py): the rule in words, every
check with its value and threshold, the inputs with their source and time, and the sizing. Every ticker an account
looks at is counted in `decision_funnel` by the first check it failed or what became of it, and up to `near_misses`
tickers a pass that failed one entry check are kept as near-misses.

An account whose `watch` row is stopped (lab/watcher.py --drill) takes no entries; its exits still run. Paper only:
the only Alpaca client here is lab/broker.py AlpacaData, which sends GET to the market data host and nothing else.
The pass reads its quotes through AlpacaStocks, stocks as a lab/market.py Venue and History over that client.
"""

import argparse
import datetime as dt
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from lab import calls, inputs
from lab.db import connect
from lab.decisions import decide, funnel, prune
from lab.grid import variant_of, variants
from lab.market import Bar, Quote, Resolution, Settled, Span, bps_fee

CONFIG = Path(__file__).resolve().parent / "stocks.toml"
LEAGUE = "stocks"   # decisions.league
VENUE = "alpaca"    # decisions.venue and the fills view's venue
SOURCE = "stock"    # the fills view's source
REGISTRY = inputs.load()
QUOTE_CHECKS = ("quote fresh", "quote valid", "spread")


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text())


class AlpacaStocks:
    """Stocks as a market (lab/market.py Venue and History) over lab/broker.py AlpacaData. Each ticker of the
    universe is one market, its one outcome the long side, priced in dollars a share. A stock never settles, so it has
    no resolutions and no settled history; its bars are its adjusted daily closes, each ending at its session's close."""

    name = VENUE

    def __init__(self, data, universe: list[str], feed: str):
        self.data, self.universe, self.feed = data, list(universe), feed

    def open_markets(self) -> Iterator[list[Quote]]:
        """Every ticker's latest quote on `feed`, in one page (one request)."""
        yield [Quote(market_id=s, outcome="long", ts=q["at"], event_id=None, series_id=None, title=s, subtitle=None,
                     market_type="stock", status="active", open_time=None, close_time=None, bid=q["bid"],
                     ask=q["ask"], last=None, bid_size=q["bid_size"], ask_size=q["ask_size"], volume=None,
                     open_interest=None)
               for s, q in self.data.quotes(self.universe, self.feed).items()]

    def resolutions(self, since: dt.datetime) -> Iterator[Resolution]:
        return iter(())

    def settled(self, cursor: str | None) -> Iterator[tuple[list[Settled], str | None]]:
        return iter(())

    def bars(self, span: Span, period_s: int) -> list[Bar]:
        """The ticker's daily closes from the span's first session to its end; daily is the only period."""
        if period_s != 86400:
            raise ValueError(f"{self.name}: daily bars only, not {period_s} s")
        start = span.open_time.date() if span.open_time else span.settled_at.date()
        closes = self.data.closes([span.market_id], start, span.settled_at).get(span.market_id, [])
        return [Bar(market_id=span.market_id, ts=calls.close_at(d), period_s=period_s, bid=None, ask=None, open=None,
                    high=None, low=None, close=c, volume=None, open_interest=None) for d, c in closes]


def account_of(family: str, variant: str) -> str:
    return f"stock-{family}-{variant[:8]}"


def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if n > 0 and len(closes) >= n else None


def check(name: str, value, threshold, ok: bool) -> dict:
    return {"name": name, "value": value, "threshold": threshold, "pass": bool(ok)}


class Trend:
    """Hold while the last close is above its `sma`-session average; sell when it closes below."""
    name = "trend"

    def __init__(self, params: dict):
        self.n = int(params["sma"])

    def rule(self) -> str:
        return f"trend: hold a ticker while its daily close is above its {self.n}-session average"

    def entry(self, closes: list[float]) -> list[dict]:
        avg = sma(closes, self.n)
        return [check("history", len(closes), self.n, avg is not None),
                check("close above average", closes[-1] if closes else None, avg,
                      avg is not None and closes[-1] > avg)]

    def exit(self, closes: list[float], held: int) -> list[dict]:
        avg = sma(closes, self.n)
        return [check("close below average", closes[-1] if closes else None, avg,
                      avg is not None and closes[-1] < avg)]


class Reversion:
    """Buy after the last `lookback` sessions fell at least `drop`; sell once the close is back above its
    `exit_sma`-session average or after `max_days` sessions held."""
    name = "reversion"

    def __init__(self, params: dict):
        self.lookback, self.drop = int(params["lookback"]), float(params["drop"])
        self.exit_n, self.max_days = int(params["exit_sma"]), int(params["max_days"])

    def rule(self) -> str:
        return (f"reversion: buy after a {self.lookback}-session fall of {self.drop:.0%} or more, sell above the "
                f"{self.exit_n}-session average or after {self.max_days} sessions")

    def entry(self, closes: list[float]) -> list[dict]:
        need = max(self.lookback + 1, self.exit_n)
        ret = closes[-1] / closes[-1 - self.lookback] - 1 if len(closes) >= need else None
        return [check("history", len(closes), need, ret is not None),
                check(f"{self.lookback}-session return", ret, -self.drop, ret is not None and ret <= -self.drop)]

    def exit(self, closes: list[float], held: int) -> list[dict]:
        avg = sma(closes, self.exit_n)
        return [check("close above average", closes[-1] if closes else None, avg,
                      avg is not None and closes[-1] > avg),
                check("sessions held", held, self.max_days, held >= self.max_days)]


FAMILIES = {"trend": Trend, "reversion": Reversion}


def rule_of(family: str, params: dict) -> str:
    """An account's rule in words; the family name when the params do not fit it."""
    try:
        return FAMILIES[family](params).rule()
    except (KeyError, TypeError, ValueError):
        return family


def league_of(cfg: dict) -> list[tuple[str, str, dict]]:
    """(account, family, params) of every variant of every family in the toml."""
    return sorted((account_of(f, variant_of(p)), f, p) for f, grid in cfg["families"].items() for p in variants(grid))


def in_session(now: dt.datetime, session: dict) -> bool:
    local = now.astimezone(ZoneInfo(session["tz"]))
    return local.weekday() < 5 and session["open"] <= local.strftime("%H:%M") < session["close"]


def quote_checks(q: Quote | None, now: dt.datetime, cfg: dict) -> list[dict]:
    """Is the quote a price to trade on: fresh, a real two-sided quote, and tight enough."""
    if q is None:
        return [check("quote fresh", None, cfg["max_quote_age_min"], False),
                check("quote valid", None, "0 < bid <= ask", False),
                check("spread", None, cfg["max_spread_bps"], False)]
    age = (now - q.ts).total_seconds() / 60
    valid = 0 < q.bid <= q.ask
    bps = (q.ask - q.bid) / ((q.ask + q.bid) / 2) * 1e4 if valid else None
    return [check("quote fresh", age, cfg["max_quote_age_min"], age <= cfg["max_quote_age_min"]),
            check("quote valid", [q.bid, q.ask], "0 < bid <= ask", valid),
            check("spread", bps, cfg["max_spread_bps"], bps is not None and bps <= cfg["max_spread_bps"])]


def read_inputs(ticker: str, bars: list[tuple[dt.date, float]], q: Quote | None) -> list[dict]:
    """The inputs a decision on the ticker read, each with its source (lab/inputs.toml) and when it was true."""
    out = []
    if bars:
        out.append({"name": "daily_close", "value": bars[-1][1], "as_of": calls.close_at(bars[-1][0])})
    if q is not None:
        out += [{"name": "stock_bid", "value": q.bid, "as_of": q.ts},
                {"name": "stock_ask", "value": q.ask, "as_of": q.ts}]
    return [{**i, "ticker": ticker, "source": inputs.source(REGISTRY, i["name"], VENUE)} for i in out]


def positions(conn, account: str) -> dict[str, dict]:
    cols = ("id", "ticker", "shares", "entered_at", "entry_price", "entry_cost")
    return {r[1]: dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM stock_positions WHERE account = %s AND exited_at IS NULL ORDER BY id",
        (account,)).fetchall()}


def cash(conn, account: str, capital: float) -> float:
    """Capital plus realized profit less what the open positions cost."""
    realized, held = conn.execute(
        "SELECT coalesce(sum(profit) FILTER (WHERE exited_at IS NOT NULL), 0), "
        "coalesce(sum(shares * entry_price + entry_cost) FILTER (WHERE exited_at IS NULL), 0) "
        "FROM stock_positions WHERE account = %s", (account,)).fetchone()
    return capital + realized - held


def sessions_held(bars: list[tuple[dt.date, float]], entered_at: dt.datetime) -> int:
    """Final daily closes since the position was entered, its entry session's included."""
    day = entered_at.astimezone(calls.ET).date()
    return sum(d >= day for d, _ in bars)


def trade(conn, account: str, fam, cfg: dict, bars: dict, quotes: dict, capital: float, bps: float,
          now: dt.datetime, entries: bool) -> dict:
    """One account's pass: exits first, then entries when `entries` (not retired, not stopped)."""
    rule, counts, near = fam.rule(), Counter(), 0
    held, left = positions(conn, account), cash(conn, account, capital)
    out = {"exits": 0, "entries": 0, "no_cash": 0, "capped": 0}
    for ticker, p in held.items():
        b, q = bars.get(ticker, []), quotes.get(ticker)
        want = fam.exit([c for _, c in b], sessions_held(b, p["entered_at"]))
        qc = quote_checks(q, now, cfg)
        if not any(c["pass"] for c in want):
            counts[(account, "held")] += 1
            continue
        if failed := [c["name"] for c in qc if not c["pass"]]:
            counts[(account, f"exit waits: {failed[0]}")] += 1
            continue
        price = q.bid
        fee = bps_fee(bps, p["shares"] * price)
        profit = p["shares"] * (price - p["entry_price"]) - p["entry_cost"] - fee
        reason = next(c["name"] for c in want if c["pass"])
        conn.execute("UPDATE stock_positions SET exited_at = %s, exit_price = %s, exit_cost = %s, profit = %s, "
                     "exit_reason = %s WHERE id = %s", (now, price, fee, profit, reason, p["id"]))
        decide(conn, LEAGUE, account, VENUE, ticker, now, "exit", rule, read_inputs(ticker, b, q), want + qc,
               {"position": p["id"], "shares": p["shares"], "price": price, "cost_bps": bps, "cost": fee,
                "entry_price": p["entry_price"], "entry_cost": p["entry_cost"], "profit": profit})
        counts[(account, "exited")] += 1
        out["exits"] += 1
        left += p["shares"] * price - fee
    open_ = len(held) - out["exits"]
    for ticker in cfg["universe"] if entries else ():
        if ticker in held:
            continue
        b, q = bars.get(ticker, []), quotes.get(ticker)
        checks = fam.entry([c for _, c in b]) + quote_checks(q, now, cfg)
        failed = [c["name"] for c in checks if not c["pass"]]
        if failed:
            counts[(account, failed[0])] += 1
            if len(failed) == 1 and near < cfg["near_misses"]:
                decide(conn, LEAGUE, account, VENUE, ticker, now, "near_miss", rule, read_inputs(ticker, b, q), checks)
                near += 1
            continue
        if open_ >= cfg["max_open"]:
            counts[(account, "max_open")] += 1
            out["capped"] += 1
            continue
        price, stake = q.ask, cfg["stake"]
        fee = bps_fee(bps, stake)
        if stake + fee > left:
            counts[(account, "no_cash")] += 1
            out["no_cash"] += 1
            continue
        shares = stake / price
        (pid,) = conn.execute(
            "INSERT INTO stock_positions (account, ticker, shares, entered_at, quoted_at, entry_price, entry_cost) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (account, ticker, shares, now, q.ts, price, fee)).fetchone()
        decide(conn, LEAGUE, account, VENUE, ticker, now, "enter", rule, read_inputs(ticker, b, q), checks,
               {"stake": stake, "price": price, "shares": shares, "cost_bps": bps, "cost": fee, "cash": left,
                "open": open_, "max_open": cfg["max_open"]}, (SOURCE, pid))
        counts[(account, "ordered")] += 1
        out["entries"] += 1
        open_ += 1
        left -= stake + fee
    funnel(conn, LEAGUE, VENUE, now, counts)
    return out


def mark(conn, now: dt.datetime, prices: dict[str, float]) -> None:
    """Each account's equity: capital, realized profit, and open positions at `prices` less their cost; a ticker
    with no price marks at its cost."""
    for account, capital in conn.execute("SELECT account, capital FROM stock_accounts").fetchall():
        realized, unrealized, n = 0.0, 0.0, 0
        for ticker, shares, entry, cost, profit, exited in conn.execute(
                "SELECT ticker, shares, entry_price, entry_cost, profit, exited_at FROM stock_positions "
                "WHERE account = %s", (account,)).fetchall():
            if exited is not None:
                realized += profit
                continue
            n += 1
            unrealized += shares * (prices.get(ticker, entry) - entry) - cost
        conn.execute("UPDATE stock_accounts SET equity = %s, realized = %s, open_positions = %s, marked_at = %s "
                     "WHERE account = %s", (capital + realized + unrealized, realized, n, now, account))


def run_pass(conn, cfg: dict, data, now: dt.datetime, ccfg: dict | None = None) -> dict:
    """One pass. `data` is an AlpacaData (closes, quotes): every ticker's closes in one batched request, and its
    quotes through AlpacaStocks.open_markets; ccfg is lab/calls.toml (settle_hours, cost_bps)."""
    ccfg = ccfg or calls.load_config()
    tickers = list(cfg["universe"])
    bars = {s: calls.final(v, now, ccfg["settle_hours"]) for s, v in data.closes(
        tickers, now.date() - dt.timedelta(days=cfg["history_days"]), now - dt.timedelta(minutes=16)).items()}
    venue = AlpacaStocks(data, tickers, cfg["quote_feed"])
    quotes = {q.market_id: q for page in venue.open_markets() for q in page} if in_session(now, cfg["session"]) else {}
    limit = dt.timedelta(minutes=cfg["max_quote_age_min"])
    is_open = any(now - q.ts <= limit for q in quotes.values())
    with conn.transaction():
        league = league_of(cfg)
        names = [a for a, _, _ in league]
        conn.cursor().executemany(
            "INSERT INTO stock_accounts (account, family, variant, params, capital, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (account) DO NOTHING",
            [(a, f, variant_of(p), Jsonb(p), cfg["capital"], now) for a, f, p in league])
        retired = [a for (a,) in conn.execute(
            "UPDATE stock_accounts SET retired_at = %s WHERE retired_at IS NULL AND NOT account = ANY(%s) "
            "RETURNING account", (now, names)).fetchall()]
        conn.execute("UPDATE stock_accounts SET retired_at = NULL WHERE account = ANY(%s)", (names,))
        stopped = {b for (b,) in conn.execute("SELECT bot FROM watch WHERE stopped_at IS NOT NULL")}
        accounts = conn.execute("SELECT account, family, params, capital, retired_at FROM stock_accounts "
                                "ORDER BY account").fetchall()
        out = {"open": is_open, "quotes": len(quotes), "retired": sorted(retired),
               "stopped": sorted(stopped & {a for a, *_ in accounts}), "accounts": {}}
        if is_open:
            for a, f, p, capital, retired_at in accounts:
                out["accounts"][a] = trade(conn, a, FAMILIES[f](p), cfg, bars, quotes, capital, ccfg["cost_bps"], now,
                                           retired_at is None and a not in stopped)
        else:
            funnel(conn, LEAGUE, VENUE, now, {(a, "market_closed"): 1 for a, *_ in accounts})
        prune(conn, LEAGUE, VENUE, now - dt.timedelta(days=cfg["trace_days"]))
        prices = {s: v[-1][1] for s, v in bars.items() if v}
        prices |= {s: q.bid for s, q in quotes.items() if all(c["pass"] for c in quote_checks(q, now, cfg))}
        mark(conn, now, prices)
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="lab.stocks", description="One stock league pass.").parse_args(argv)
    data = calls.client()
    if data is None:
        sys.exit("stocks: no ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY")
    with connect(autocommit=True) as conn:
        out = run_pass(conn, load_config(), data, dt.datetime.now(dt.timezone.utc))
    print(f"stocks: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
