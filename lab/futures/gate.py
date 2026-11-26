#!/usr/bin/env python3
"""The futures gate: the long-short research strategies of futures.toml [gate] on okx swap candles (lab/futures/research/bust.py),
each trade net of the funding recorded for one venue, judged by lab/events/families.py verdict() with the suggest.toml
[confidence] thresholds. One readiness row per strategy per venue of lab/futures/funding.py VENUES, market `futures`.

  python -m lab.futures.gate <userdir>    # by hand inside the image, DATABASE_URL set; downloads into userdir once

A venue's row reads only the trades its own funding series covers, from the series' first settlement on, with no funding guessed
for the rest (okx's series is months old, kucoin's starts 2024). Those trades are cut into `window_days` windows by open day; the
newest `lockbox_windows` are the lockbox, read only by a strategy passing every other gate. PBO ranks the strategies of a venue.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from lab import metrics
from lab.core import readiness
from lab.core.db import connect
from lab.crypto import suggest
from lab.events.families import daily_of, verdict
from lab.futures import funding
from lab.futures.paper import load_config
from lab.futures.research import bust

UTC = dt.timezone.utc


def net(trades: pd.DataFrame, rates: pd.DataFrame, venue: str) -> pd.DataFrame:
    """The trades `venue`'s funding covers, oldest first, with `net`: profit after that funding."""
    t = bust.measured(trades, rates, (venue,))
    t = t[t["funding"].notna()].sort_values("open_date")
    return t.assign(net=t["gross"] + t["funding"])


def windows(trades: pd.DataFrame, days: int, wallet: float) -> list[dict]:
    """`trades` (from net()) as consecutive `days`-day windows from the first open day: each trade's return on its stake,
    its open day as the event (the basket's coins open together), and the window's net profit by close day on `wallet`."""
    if trades.empty:
        return []
    first = trades["open_date"].min().normalize()
    out = []
    for k, w in trades.groupby(((trades["open_date"] - first).dt.days // days).to_numpy()):
        start = first + pd.Timedelta(days=days * int(k))
        by_day = w.groupby(w["close_date"].dt.normalize())["net"].sum() / wallet
        span = pd.date_range(start, max(start + pd.Timedelta(days=days - 1), by_day.index.max()), freq="D")
        out.append({"start_at": start.to_pydatetime().replace(tzinfo=UTC),
                    "returns": (w["net"] / w["stake_amount"]).tolist(),
                    "events": [d.isoformat() for d in w["open_date"].dt.date],
                    "daily": by_day.reindex(span, fill_value=0.0).tolist()})
    return out


def judge(by_strategy: dict[str, list[dict]], trials: int, ccfg: dict, gcfg: dict, venue: str) -> dict[str, dict]:
    """{strategy: verdict} over its windows; the newest `lockbox_windows` are read only by one passing every other gate."""
    lock_n = gcfg["lockbox_windows"]
    split = {s: (w[:-lock_n], w[-lock_n:]) if len(w) > lock_n else (w, []) for s, w in by_strategy.items()}
    panel = metrics.pbo_panel({s: daily_of(test) for s, (test, _) in split.items()}, gcfg["pbo_blocks"])
    pbo = None if panel is None else panel["pbo"]

    def one(s: str, lock):
        return verdict(split[s][0], lock, pbo, trials, ccfg, metrics.seed_of("futures", venue, s, "readiness"))

    out = {s: one(s, None) for s in split}
    for s, r in out.items():
        if all(g["pass"] for g in r["gates"][:-1]):
            out[s] = one(s, split[s][1])
    return out


def gate(conn, trades: dict[str, pd.DataFrame], cfg: dict, ccfg: dict, wallet: float) -> dict[str, int]:
    """Write a readiness row per strategy in `trades` (each its backtest trades, bust.backtest()) per venue; returns the
    ready count per venue."""
    g = cfg["gate"]
    pairs = sorted({p for t in trades.values() for p in t["pair"]})
    rates = bust.load_funding(conn, pairs)
    trials = conn.execute("SELECT count(*) FROM trials WHERE strategy = ANY(%s)", (g["trial_strategies"],)).fetchone()[0]
    out = {}
    for venue in funding.VENUES:
        result = judge({s: windows(net(t, rates, venue), g["window_days"], wallet) for s, t in trades.items()},
                       trials, ccfg, g, venue)
        rows = [{**{k: r[k] for k in ("windows", "trades", "expectancy", "expectancy_lo", "expectancy_hi", "dsr", "pbo",
                                      "trials", "ready", "passed", "gates")}, "venue": venue, "strategy": s}
                for s, r in result.items()]
        readiness.store(conn, readiness.FUTURES, rows, venue=venue)
        out[venue] = sum(r["ready"] for r in result.values())
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lab.futures.gate", description="The readiness gate of the futures research strategies.")
    p.add_argument("userdir", help="freqtrade userdir the okx swap candles download into, once")
    user = Path(p.parse_args(argv).userdir).resolve()
    cfg = load_config()
    conf = bust.download(user)
    trades = {}
    for s in cfg["gate"]["strategies"]:
        parts = []
        for coins in bust.BASKETS.values():
            bust.config(conf, coins)
            parts.append(bust.backtest(user, conf, s))
        trades[s] = pd.concat(parts, ignore_index=True)
    with connect(autocommit=True) as conn:
        out = gate(conn, trades, cfg, suggest.load_config()["confidence"], bust.WALLET * len(bust.BASKETS))
    print(out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
