#!/usr/bin/env python3
"""The stock league's walk-forward backtest: each family variant of lab/stocks/stocks.toml walks forward over daily
Alpaca closes, window by window, and each (variant, window) is stored as one market_backtests row (venue alpaca).

  python -m lab.stocks.backtest     # every variant on the last `[backtest] windows` windows

The rules are lab/stocks/paper.py's: the same Trend and Reversion entry and exit checks, with `sessions_held` for the
hold count, so there is no second copy. A decision on session d reads only the closes final at the paper pass's
time (calls.final: a bar counts settle_hours after its 16:00 ET close) of session d-1, and fills at session d's close,
the stand-in for the quote the pass trades on. Exits run before entries, an exit sells and an entry buys at that
close, each side paying lab/inputs/calls.toml cost_bps on its notional, and an entry takes `[paper] stake` unless
`max_open` or the cash stops it. A position still open at a window's end is not a trade (open_fills). Reading a bar
that is later than the decision's time raises Lookahead (Tape.read). Results go through lab/events/backtest.py
evaluate, the statistics the event-contract backtests use, and the trial register: each (variant, window) is a trial
of exchange alpaca. A trade's event is its close session: trades closing together move together, and
the gate (lab/stocks/gate.py) resamples whole sessions, since the 10-ticker universe is under the 20 clusters an interval needs.
"""

import argparse
import datetime as dt
import sys
from dataclasses import dataclass

import pandas as pd
from psycopg.types.json import Jsonb

from lab import metrics
from lab.core.db import connect
from lab.core.grid import variant_of
from lab.crypto import suggest
from lab.events.backtest import Lookahead, evaluate
from lab.inputs import calls
from lab.market import bps_fee
from lab.stocks import paper

__all__ = ["Lookahead", "Tape", "walk", "run", "main"]


class Tape:
    """Daily closes per ticker, readable only as of a time: the one way a backtest reads a price."""

    def __init__(self, closes: dict[str, list[tuple[dt.date, float]]], settle_hours: float):
        self.bars = {s: dict(v) for s, v in closes.items()}
        self.settle = dt.timedelta(hours=settle_hours)
        self.sessions = sorted({d for v in self.bars.values() for d in v})

    def read(self, ticker: str, session: dt.date, at: dt.datetime) -> float:
        """The session's close, printed at its 16:00 ET close; a read before that is lookahead."""
        if calls.close_at(session) > at:
            raise Lookahead(f"{ticker} {session} closes at {calls.close_at(session)}, after {at}")
        return self.bars[ticker][session]

    def final(self, ticker: str, at: dt.datetime) -> list[tuple[dt.date, float]]:
        """The ticker's closes final at `at`, as the paper pass reads them (calls.final), each through read."""
        return [(d, self.read(ticker, d, at)) for d in sorted(self.bars[ticker])
                if at >= calls.close_at(d) + self.settle]


@dataclass
class Result:
    trades: pd.DataFrame
    orders: int
    open_fills: int


def walk(fam, tape: Tape, cfg: dict, cost_bps: float, start: dt.date, end: dt.date) -> Result:
    """One variant over the sessions start to end (end exclusive), from the paper account's capital."""
    pc, bars, held, left, trades, orders = cfg["paper"], {}, {}, cfg["paper"]["capital"], [], 0
    for day in (d for d in tape.sessions if start <= d < end):
        at = calls.close_at(day)                       # the fill: this session's close
        decided = at - dt.timedelta(seconds=1)         # the decision: only what was final before the close
        hist = {s: tape.final(s, decided) for s in cfg["universe"] if s in tape.bars}
        for ticker, p in list(held.items()):
            b = hist.get(ticker, [])
            p["low"] = min(p["low"], tape.read(ticker, day, at))   # the lowest close the position lived through
            if any(c["pass"] for c in fam.exit([c for _, c in b], paper.sessions_held(b, p["at"]))):
                price = tape.read(ticker, day, at)
                fee = bps_fee(cost_bps, p["shares"] * price)
                profit = p["shares"] * (price - p["price"]) - p["fee"] - fee
                trades.append({"ticker": ticker, "open_date": p["at"], "close_date": at, "open_rate": p["price"],
                               "close_rate": price, "min_rate": p["low"], "profit_abs": profit,
                               "profit_ratio": profit / (p["price"] * p["shares"] + p["fee"])})
                left += p["shares"] * price - fee
                del held[ticker]
        for ticker in cfg["universe"]:
            if ticker in held or ticker not in tape.bars or day not in tape.bars[ticker]:
                continue
            if not all(c["pass"] for c in fam.entry([c for _, c in hist[ticker]])):
                continue
            price, stake = tape.read(ticker, day, at), pc["stake"]
            fee = bps_fee(cost_bps, stake)
            if len(held) >= pc["max_open"] or stake + fee > left:
                continue
            held[ticker] = {"at": at, "price": price, "shares": stake / price, "fee": fee, "low": price}
            left -= stake + fee
            orders += 1
    cols = ["ticker", "open_date", "close_date", "open_rate", "close_rate", "min_rate", "profit_abs", "profit_ratio"]
    return Result(pd.DataFrame(trades, columns=cols), orders, len(held))


def windows(today: dt.date, n: int, days: int) -> list[tuple[dt.date, dt.date]]:
    """The last n windows of `days` calendar days, the newest ending today, oldest first."""
    return [(today - dt.timedelta(days=days * (i + 1)), today - dt.timedelta(days=days * i)) for i in reversed(range(n))]


def store(conn, family: str, params: dict, res: Result, start: dt.date, end: dt.date, cfg: dict, ccfg: dict) -> dict:
    variant, venue = variant_of(params), paper.VENUE
    s, e = (dt.datetime.combine(d, dt.time(), dt.timezone.utc) for d in (start, end))
    conn.execute("INSERT INTO trials (strategy, exchange, variant) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                 (family, venue, f"{variant}:{start}"))
    trials = conn.execute("SELECT count(*) FROM trials WHERE exchange = %s", (venue,)).fetchone()[0]
    t = res.trades.assign(open_date=pd.to_datetime(res.trades["open_date"], utc=True),
                          close_date=pd.to_datetime(res.trades["close_date"], utc=True))
    out = evaluate(t, s, e, cfg["paper"]["capital"], metrics.seed_of(family, venue, variant, "confidence"), ccfg, trials,
                   event=False)
    ev = out["interval"]
    conn.execute(
        """INSERT INTO market_backtests (family, venue, variant, params, start_at, end_at, orders, fills, trades,
               open_fills, metrics, returns, events, daily, expectancy, expectancy_lo, expectancy_hi, dsr, trials,
               computed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (family, venue, variant, start_at) DO UPDATE SET params = EXCLUDED.params,
               end_at = EXCLUDED.end_at, orders = EXCLUDED.orders, fills = EXCLUDED.fills, trades = EXCLUDED.trades,
               open_fills = EXCLUDED.open_fills, metrics = EXCLUDED.metrics, returns = EXCLUDED.returns,
               events = EXCLUDED.events, daily = EXCLUDED.daily, expectancy = EXCLUDED.expectancy,
               expectancy_lo = EXCLUDED.expectancy_lo, expectancy_hi = EXCLUDED.expectancy_hi, dsr = EXCLUDED.dsr,
               trials = EXCLUDED.trials, computed_at = now()""",
        (family, venue, variant, Jsonb(params), s, e, res.orders, res.orders, len(t), res.open_fills,
         Jsonb(out["metrics"]), ev["returns"].tolist(), t["close_date"].dt.strftime("%Y-%m-%d").tolist(), out["daily"].tolist(), ev["mean"],
         ev["lo"], ev["hi"], out["dsr"], trials))
    return {"orders": res.orders, "trades": len(t), "open": res.open_fills, "expectancy": ev["mean"]}


def run(conn, data, cfg: dict, today: dt.date, ccfg: dict | None = None, confidence: dict | None = None) -> dict:
    """Every variant on every window; `data` is an AlpacaData (closes). Returns {(account, window start): summary}."""
    ccfg = ccfg or calls.load_config()
    confidence = confidence or suggest.load_config()["confidence"]
    bt = cfg["backtest"]
    wins = windows(today, bt["windows"], bt["window_days"])
    closes = data.closes(list(cfg["universe"]), wins[0][0] - dt.timedelta(days=bt["warmup_days"]),
                         dt.datetime.combine(today, dt.time(), dt.timezone.utc))
    tape = Tape(closes, ccfg["settle_hours"])
    out = {}
    for account, family, params in paper.league_of(cfg):
        for start, end in wins:
            res = walk(paper.FAMILIES[family](params), tape, cfg, ccfg["cost_bps"], start, end)
            out[(account, start)] = store(conn, family, params, res, start, end, cfg, confidence)
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="lab.stocks.backtest", description="Walk-forward backtest of the stock variants."
                            ).parse_args(argv)
    data = calls.client()
    if data is None:
        sys.exit("stocks: no ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY")
    with connect(autocommit=True) as conn:
        out = run(conn, data, paper.load_config(), dt.datetime.now(calls.ET).date())
    for (account, start), r in out.items():
        print(f"{account} {start}: {r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
