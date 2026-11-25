#!/usr/bin/env python3
"""The futures league: paper accounts long or short on okx and kucoin perpetual swaps, filled at the exchange's public
ticker. It is a market adapter of the one paper pass (lab/stages/paper.py, Futures below); its accounts and positions are
paper_accounts (venue futures) and paper_orders rows, a position one order whose side is long or short and whose
market_id is `<exchange>:<swap symbol>`.

  python -m lab.futures.paper     # one pass

Every family variant of lab/futures/futures.toml is one account with `[paper] capital`; a variant no longer in the toml is
retired: it takes no entries and its open positions still exit and mark. A pass:
1. Reads the ticker of every swap lab/futures/funding.py has recorded funding for, before the transaction opens.
2. Exits first: a position held `hold_hours` closes at the opposite side of a fresh, valid, tight quote (a long sells at the
   bid, a short buys at the ask).
3. Entries: a swap whose latest recorded funding rate is fresh and at least `threshold` opens a short at the bid; at most
   minus `threshold`, a long at the ask. `stake` of notional, unlevered; the account's cash must pay it.
4. Each side pays `taker_bps` on its notional. Every funding settlement in funding_rates between the entry and the exit is
   charged on the entry notional: a long pays the rate, a short receives it. The profit on the order is net of both fees
   and all funding.
5. Marks each account: capital, realized profit, and each open position at its exit price less its entry fee. Funding
   accrued on an open position is not in the mark; it lands in profit when the position closes.
Every entry and exit is a `futures` decision. An account whose `watch` row is stopped takes no entries.
Paper only: the only exchange client here reads tickers, and nothing in this module can reach an order API.
"""

import argparse
import datetime as dt
import sys
import tomllib
from collections import Counter, namedtuple
from pathlib import Path

from lab.core.db import connect
from lab.core.decisions import decide, funnel
from lab.core.grid import variant_of, variants
from lab.market import bps_fee
from lab.stages import paper as stage

CONFIG = Path(__file__).resolve().parent / "futures.toml"
LEAGUE = "futures"   # decisions.league
VENUE = "futures"    # paper_accounts.venue and decisions.venue
SOURCE = "paper"     # the fills view's source: one paper ledger
FAMILIES = ("funding",)
CCXT_ID = {"okx": "okx", "kucoin": "kucoinfutures"}

Quote = namedtuple("Quote", "bid ask ts")


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text())


class Tickers:
    """The exchanges' public tickers through ccxt, GET only: {(exchange, swap symbol): Quote}."""

    def __init__(self, exchanges: list[str]):
        import ccxt
        self.ex = {e: getattr(ccxt, CCXT_ID[e])({"enableRateLimit": True}) for e in exchanges}

    def quotes(self, symbols: dict[str, list[str]]) -> dict[tuple[str, str], Quote]:
        out = {}
        for exchange, names in symbols.items():
            for s, t in self.ex[exchange].fetch_tickers(names).items():
                if t.get("bid") and t.get("ask") and t.get("timestamp"):
                    out[(exchange, s)] = Quote(t["bid"], t["ask"],
                                               dt.datetime.fromtimestamp(t["timestamp"] / 1000, dt.timezone.utc))
        return out


def account_of(family: str, variant: str) -> str:
    return f"futures-{family}-{variant[:8]}"


def market_of(exchange: str, symbol: str) -> str:
    return f"{exchange}:{symbol}"


def league_of(cfg: dict) -> list[tuple[str, str, dict]]:
    return sorted((account_of(f, variant_of(p)), f, p) for f, grid in cfg["families"].items() for p in variants(grid))


def check(name: str, value, threshold, ok: bool) -> dict:
    return {"name": name, "value": value, "threshold": threshold, "pass": bool(ok)}


def rule_of(params: dict) -> str:
    return (f"funding: short a swap whose funding rate is at least {params['threshold']:.4%}, long one at or below its "
            f"negative, close after {params['hold_hours']} h")


def quote_checks(q: Quote | None, now: dt.datetime, cfg: dict) -> list[dict]:
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


def funding_paid(conn, market_id: str, side: str, notional: float, since: dt.datetime, until: dt.datetime) -> float:
    """What a position pays in funding between `since` (exclusive) and `until`: a long pays the recorded rate on its
    notional at each settlement, a short receives it, so a negative result is income."""
    exchange, symbol = market_id.split(":", 1)
    total = conn.execute("SELECT coalesce(sum(rate), 0) FROM funding_rates WHERE exchange = %s AND pair = %s "
                         "AND ts > %s AND ts <= %s", (exchange, symbol, since, until)).fetchone()[0]
    return notional * total * (1 if side == "long" else -1)


def inputs_of(market_id: str, q: Quote | None, rate: float | None, rate_at: dt.datetime | None) -> list[dict]:
    out = []
    if rate is not None:
        out.append({"name": "funding_rate", "value": rate, "as_of": rate_at, "source": "ccxt funding history"})
    if q is not None:
        out += [{"name": "swap_bid", "value": q.bid, "as_of": q.ts, "source": "ccxt ticker"},
                {"name": "swap_ask", "value": q.ask, "as_of": q.ts, "source": "ccxt ticker"}]
    return [{**i, "instrument": market_id} for i in out]


def trade(conn, account: str, params: dict, cfg: dict, quotes: dict, universe: list[tuple[str, str]], capital: float,
          now: dt.datetime, entries: bool) -> dict:
    pc = cfg["paper"]
    bps, rule, counts, near = pc["taker_bps"], rule_of(params), Counter(), 0
    hold = dt.timedelta(hours=params["hold_hours"])
    held, left = stage.open_orders(conn, account), stage.cash(conn, account, capital)
    out = {"exits": 0, "entries": 0, "no_cash": 0, "capped": 0}
    for market_id, p in held.items():
        q = quotes.get(tuple(market_id.split(":", 1)))
        age = now - p["sent_at"]
        want = [check("held long enough", age.total_seconds() / 3600, params["hold_hours"], age >= hold)]
        qc = quote_checks(q, now, pc)
        if not want[0]["pass"]:
            counts[(account, "held")] += 1
            continue
        if failed := [c["name"] for c in qc if not c["pass"]]:
            counts[(account, f"exit waits: {failed[0]}")] += 1
            continue
        sign = 1 if p["side"] == "long" else -1
        price, n = (q.bid if sign == 1 else q.ask), p["contracts"]
        fee = bps_fee(bps, n * price)
        funding = funding_paid(conn, market_id, p["side"], n * p["price"], p["sent_at"], now)
        profit = sign * n * (price - p["price"]) - p["fee"] - fee - funding
        stage.close(conn, [(now, price, fee, profit, want[0]["name"], p["id"])])
        decide(conn, LEAGUE, account, VENUE, market_id, now, "exit", rule, inputs_of(market_id, q, None, None), want + qc,
               {"position": p["id"], "side": p["side"], "contracts": n, "price": price, "cost_bps": bps, "cost": fee,
                "entry_price": p["price"], "entry_cost": p["fee"], "funding": funding, "profit": profit})
        counts[(account, "exited")] += 1
        out["exits"] += 1
        left += n * p["price"] + profit + p["fee"]
    open_ = len(held) - out["exits"]
    for exchange, symbol in universe if entries else ():
        market_id = market_of(exchange, symbol)
        if market_id in held:
            continue
        q = quotes.get((exchange, symbol))
        rate_at, rate = conn.execute("SELECT ts, rate FROM funding_rates WHERE exchange = %s AND pair = %s "
                                     "ORDER BY ts DESC LIMIT 1", (exchange, symbol)).fetchone()
        age_h = (now - rate_at).total_seconds() / 3600
        th = params["threshold"]
        checks = [check("funding fresh", age_h, pc["max_funding_age_h"], age_h <= pc["max_funding_age_h"]),
                  check("funding beyond threshold", rate, th, abs(rate) >= th)] + quote_checks(q, now, pc)
        failed = [c["name"] for c in checks if not c["pass"]]
        if failed:
            counts[(account, failed[0])] += 1
            if len(failed) == 1 and near < pc["near_misses"]:
                decide(conn, LEAGUE, account, VENUE, market_id, now, "near_miss", rule,
                       inputs_of(market_id, q, rate, rate_at), checks)
                near += 1
            continue
        if open_ >= pc["max_open"]:
            counts[(account, "max_open")] += 1
            out["capped"] += 1
            continue
        side = "short" if rate > 0 else "long"
        price, stake = (q.bid if side == "short" else q.ask), pc["stake"]
        fee = bps_fee(bps, stake)
        if stake + fee > left:
            counts[(account, "no_cash")] += 1
            out["no_cash"] += 1
            continue
        n = stake / price
        pid = stage.enter(conn, account, market_id, side, stake, None, now, q.ts, q.bid, q.ask, n, price, fee)
        decide(conn, LEAGUE, account, VENUE, market_id, now, "enter", rule, inputs_of(market_id, q, rate, rate_at),
               checks, {"stake": stake, "side": side, "price": price, "contracts": n, "cost_bps": bps, "cost": fee,
                        "cash": left, "open": open_, "max_open": pc["max_open"]}, (SOURCE, pid))
        counts[(account, "ordered")] += 1
        out["entries"] += 1
        open_ += 1
        left -= stake + fee
    funnel(conn, LEAGUE, VENUE, now, counts)
    return out


class Futures:
    """The futures league as a lab/stages/paper.py adapter; the tickers are read before the transaction opens."""
    league, venue = LEAGUE, VENUE
    exits_retired = True   # a retired or stopped account still closes what it holds

    def __init__(self, cfg: dict, quotes: dict, universe: list[tuple[str, str]], now: dt.datetime):
        self.cfg, self.quotes, self.universe = cfg, quotes, universe
        self.capital, self.trace_days = cfg["paper"]["capital"], cfg["paper"]["trace_days"]
        limit = dt.timedelta(minutes=cfg["paper"]["max_quote_age_min"])
        self.fresh = {k: q for k, q in quotes.items() if now - q.ts <= limit and 0 < q.bid <= q.ask}

    def accounts(self, conn):
        return league_of(self.cfg)

    def prepare(self, conn, now, league):
        return None

    def idle(self, ctx):
        return None if self.fresh else "no_quotes"

    def report(self, ctx):
        return {"quotes": len(self.fresh)}

    def trade(self, conn, ctx, account, family, params, capital, entries, now):
        return trade(conn, account, params, self.cfg, self.quotes, self.universe, capital, now, entries)

    def prices(self, conn, ctx, held):
        """A position marks at its exit price (a long at the bid, a short at the ask) of a fresh quote, else its cost."""
        out = {}
        for m, side in held:
            q = self.fresh.get(tuple(m.split(":", 1)))
            if q is not None:
                out[(m, side)] = q.bid if side == "long" else q.ask
        return out

    def after(self, conn, ctx, now):
        return {}


def universe_of(conn, cfg: dict) -> list[tuple[str, str]]:
    """Every (exchange, swap symbol) of the configured venues that has a recorded funding rate."""
    return [tuple(r) for r in conn.execute("SELECT DISTINCT exchange, pair FROM funding_rates WHERE exchange = ANY(%s) "
                                           "ORDER BY 1, 2", (cfg["venues"],)).fetchall()]


def run_pass(conn, cfg: dict, tickers, now: dt.datetime) -> dict:
    """One pass. `tickers.quotes({exchange: [symbol]})` returns {(exchange, symbol): Quote}."""
    universe = universe_of(conn, cfg)
    by_exchange: dict[str, list[str]] = {}
    for e, s in universe:
        by_exchange.setdefault(e, []).append(s)
    return stage.run_pass(conn, Futures(cfg, tickers.quotes(by_exchange), universe, now), now)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="lab.futures.paper", description="One futures league pass.").parse_args(argv)
    cfg = load_config()
    with connect(autocommit=True) as conn:
        out = run_pass(conn, cfg, Tickers(cfg["venues"]), dt.datetime.now(dt.timezone.utc))
    print(f"futures: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
