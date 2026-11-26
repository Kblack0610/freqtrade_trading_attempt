#!/usr/bin/env python3
"""The stock gate: each family variant's walk-forward windows (lab/stocks/backtest.py) pooled before the lockbox and judged
by lab/events/families.py verdict() with the suggest.toml [confidence] thresholds, one readiness row per variant.

  python -m lab.stocks.gate     # read the newest backtest run, write the `stocks` rows of readiness

A run is the last `[backtest] windows` windows ending at its newest window; older runs overlap it and are not read. The
lockbox is the windows starting in the newest `[backtest] lockbox_months` months (30 days each) of that run, and a variant
reads it only when every other gate passes. PBO ranks the variants of a family against each other over `pbo_blocks` blocks.
"""

import argparse
import datetime as dt
import sys
from collections import defaultdict

from lab import metrics
from lab.core import readiness
from lab.core.db import connect
from lab.core.grid import variant_of, variants
from lab.crypto import suggest
from lab.events.families import daily_of, day, verdict
from lab.stocks import paper

COLS = ("family", "variant", "start_at", "end_at", "returns", "events", "daily")
ROWS = f"SELECT {', '.join(COLS)} FROM market_backtests WHERE venue = %s"
MONTH_DAYS = 30


def split(rows: list[dict], bt: dict) -> tuple[list[dict], list[dict]]:
    """(test, lockbox) windows of the newest run among `rows`, oldest first."""
    if not rows:
        return [], []
    newest = max(day(r["start_at"]) for r in rows)
    run = {newest - dt.timedelta(days=bt["window_days"] * i) for i in range(bt["windows"])}
    rows = sorted((r for r in rows if day(r["start_at"]) in run), key=lambda r: r["start_at"])
    cut = newest + dt.timedelta(days=bt["window_days"] - MONTH_DAYS * bt["lockbox_months"])
    return [r for r in rows if day(r["start_at"]) < cut], [r for r in rows if day(r["start_at"]) >= cut]


def judge(by_variant: dict[str, tuple[list[dict], list[dict]]], trials: int, ccfg: dict, blocks: int, family: str,
          venue: str = paper.VENUE) -> dict[str, dict]:
    """{variant: verdict} for one family; the lockbox is read only by a variant passing every other gate."""
    panel = metrics.pbo_panel({v: daily_of(test) for v, (test, _) in by_variant.items()}, blocks)
    pbo = None if panel is None else panel["pbo"]

    def one(v: str, lock):
        return verdict(by_variant[v][0], lock, pbo, trials, ccfg, metrics.seed_of(family, venue, v, "readiness"))

    out = {v: one(v, None) for v in by_variant}
    for v, r in out.items():
        if all(g["pass"] for g in r["gates"][:-1]):
            out[v] = one(v, by_variant[v][1])
    return out


def gate(conn, cfg: dict, ccfg: dict) -> dict[str, int]:
    """Write each current variant's readiness row; returns the ready count per family."""
    venue, bt = paper.VENUE, cfg["backtest"]
    found = defaultdict(list)
    for r in conn.execute(ROWS, (venue,)).fetchall():
        r = dict(zip(COLS, r))
        found[(r["family"], r["variant"])].append(r)
    trials = conn.execute("SELECT count(*) FROM trials WHERE exchange = %s", (venue,)).fetchone()[0]
    out, rows = {}, []
    for family, grid in cfg["families"].items():
        params = {variant_of(p): p for p in variants(grid)}
        windows = {v: split(found[(family, v)], bt) for v in params}
        result = judge(windows, trials, ccfg, bt["pbo_blocks"], family)
        rows += [{**{k: r[k] for k in ("windows", "trades", "expectancy", "expectancy_lo", "expectancy_hi", "dsr", "pbo",
                                       "trials", "ready", "passed", "gates")},
                  "venue": venue, "strategy": f"{family}/{v}", "family": family, "variant": v, "params": params[v]}
                 for v, r in result.items()]
        out[family] = sum(r["ready"] for r in result.values())
    readiness.store(conn, readiness.STOCKS, rows, venue=venue)
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="lab.stocks.gate", description="The readiness gate of the stock variants."
                            ).parse_args(argv)
    with connect(autocommit=True) as conn:
        out = gate(conn, paper.load_config(), suggest.load_config()["confidence"])
    print(out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
