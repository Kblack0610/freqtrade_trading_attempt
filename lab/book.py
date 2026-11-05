#!/usr/bin/env python3
"""The strategy book: what each scored strategy might return, beside holding BTC, and what a stage costs.

Advice for a human, like lab/confidence.py: nothing here touches a bot, and lab/league.py refuses real money.
A strategy gets a range only with a confidence score and at least [confidence] min_folds walk-forward months.
Its basis is those months' tuned wallet returns, the months hyperopt never saw, resampled in runs of
[book] block_months. Hold is BTC over the same months. A range assumes the tested months repeat: it says
nothing about a market they did not contain, and 12 months cannot carry a 5-year range with any firmness.

Run alone with `python lab/book.py`; lab/analyze.py and lab/walkforward.py run it after the score.
"""

import sys

import pandas as pd

import metrics
import suggest
from db import connect

FOLDS = """
SELECT w.strategy, w.exchange, w.fold_start, w.test_end, coalesce(w.tuned_profit, 0)
FROM walkforward w JOIN confidence c USING (strategy, exchange)
WHERE c.score IS NOT NULL AND w.ok AND w.params IS NOT NULL ORDER BY 1, 2, 3
"""
COLS = ["strategy", "exchange", "basis", "horizon_months", "p5", "p50", "p95", "prob_loss", "exp_max_drawdown", "paths",
        "basis_months"]


def hold_months(folds: list[tuple], close: pd.Series) -> list[float]:
    """BTC's return over each fold's month, or nothing when a month has no close to start from."""
    day = pd.Timedelta(days=1)
    start = [close.asof(pd.Timestamp(f[0]) - day) for f in folds]
    end = [close.asof(pd.Timestamp(f[1]) - day) for f in folds]
    if pd.isna(start).any() or pd.isna(end).any():
        return []
    return [b / a - 1.0 for a, b in zip(start, end)]


def rows(folds: dict, close: pd.Series, cfg: dict) -> list[dict]:
    """folds: (strategy, exchange) -> [(fold_start, test_end, monthly return)] for scored pairs, in month order."""
    c = cfg["book"]
    out = []
    for (s, e), f in folds.items():
        if len(f) < cfg["confidence"]["min_folds"]:
            continue
        for basis, monthly in (("strategy", [x[2] for x in f]), ("hold", hold_months(f, close))):
            for h in c["horizon_months"] if monthly else ():
                out.append({"strategy": s, "exchange": e, "basis": basis, "horizon_months": h, "basis_months": len(monthly),
                            **metrics.projection(monthly, h, metrics.seed_of(s, e, basis, h), block=c["block_months"])})
    return out


def budget(cfg: dict) -> dict:
    """Each stage's capital and what it can lose at each limit, and the yearly cost of running at all."""
    c = cfg["book"]
    vps = 12 * c["vps_per_month"]
    stages = [{**s, "at_risk": s["stake"] * s["max_open"], "day_loss": s["capital"] * cfg["watcher"]["day_loss"],
               "hard_stop": s["capital"] * c["hard_stop"], "vps_share": vps / s["capital"]} for s in c["stages"]]
    return {"stages": stages, "vps_year": vps, "cpa_once": c["cpa_once"]}


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    folds: dict = {}
    for s, e, start, end, profit in conn.execute(FOLDS):
        folds.setdefault((s, e), []).append((start, end, profit))
    days = conn.execute("SELECT day, btc_close FROM regimes ORDER BY day").fetchall()
    close = pd.Series([c for _, c in days], index=pd.to_datetime([d for d, _ in days]), dtype=float)
    out = rows(folds, close, cfg)
    with conn.transaction():
        conn.execute("DELETE FROM book")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO book ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
    return {"book": len({(r["strategy"], r["exchange"]) for r in out})}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"book: {run(conn)}", flush=True)
    sys.exit(0)
