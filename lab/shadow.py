#!/usr/bin/env python3
"""Shadow paper accounts: the continuous promote, mark and retire loop, as database rows instead of pods.

A series is a strategy x venue as it stands or under one setup overlay (lab/variants.toml), and the overlay id keys
everything here, so the overlay grid is the spawn: each overlay series the nightly sweep backtests is a candidate.
A series earns an account when its walk-forward out-of-sample edge and its backtest edge both clear
lab/suggest.toml [shadow], it never had one before, and its BTC-residual daily returns (lab/diversity.residuals)
correlate at |rho| max_rho or under with every active account of its cohort, best Sharpe low end first, until the
cohort is full. The cohort is its holding period: short under hold_edge_hours for the median trade, long otherwise.

Each night appends the account's days after its promotion day from strategy_daily (overlay_daily for an overlay
series), the backtest that lab/analyze.py reruns through yesterday (an overlay series with an open account reruns
outside the [sweep] cap): days nobody had seen when it was promoted, replayed on candles, not filled
on a book. A mark is never rewritten. Then the written rule: retire past the cohort's drawdown, when the
block-bootstrap interval of the mean daily return is below zero after min_days, or at max_days without winning;
winning when, after min_days, it is up and the interval's low end is above zero. A winner that fails the rule
retires. A retired series never comes back, and its slot is refilled the same night.

lab/analyze.py runs this after horizon; `python -m lab.shadow` runs it alone. Paper only: nothing here trades.
"""

import datetime as dt
import sys

import numpy as np
import pandas as pd
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lab import diversity, metrics, suggest
from lab.db import connect

GATED = "win, sharpe_lo, expectancy_lo, trades, median_hold_hours"
CANDIDATES = (f"SELECT w.*, c.oos_trades, c.oos_expectancy FROM (SELECT strategy, exchange, '' AS overlay, {GATED} "
              f"FROM strategy_windows UNION ALL SELECT strategy, exchange, overlay, {GATED} FROM overlay_windows) w "
              "JOIN confidence c USING (strategy, exchange) WHERE w.win = %s ORDER BY w.strategy, w.exchange, w.overlay")
ACCOUNTS = "SELECT * FROM shadow_accounts ORDER BY account"
# The last day each series covers: chunks end exclusive, the current month's at today.
THROUGH = "SELECT strategy, exchange, overlay, max(chunk_end) - 1 FROM backtest_chunks WHERE ok GROUP BY 1, 2, 3"
# Every series' daily returns, as-is with overlay ''.
DAILY = ("(SELECT strategy, exchange, '' AS overlay, day, ret FROM strategy_daily "
         "UNION ALL SELECT strategy, exchange, overlay, day, ret FROM overlay_daily) d")
KEY = ("strategy", "exchange", "overlay")


def name_of(strategy: str, exchange: str, overlay: str = "") -> str:
    return f"shadow-{strategy}-{exchange}" + (f"-{overlay}" if overlay else "")


def cohort_of(hold_hours: float, cfg: dict) -> str:
    return "short" if hold_hours < cfg["hold_edge_hours"] else "long"


def eligible(c: dict, cfg: dict) -> bool:
    """Pure: the entry gates, each a walk-forward or backtest edge with enough trades behind it. Walk-forward
    scores the strategy x venue as it stands, so an overlay series carries its base's out-of-sample edge."""
    return (c["median_hold_hours"] is not None
            and (c["trades"] or 0) >= cfg["min_backtest_trades"]
            and c["expectancy_lo"] is not None and c["expectancy_lo"] > cfg["expectancy_lo_above"]
            and (c["oos_trades"] or 0) >= cfg["min_oos_trades"]
            and c["oos_expectancy"] is not None and c["oos_expectancy"] > cfg["oos_expectancy_above"])


def promote(candidates: list[dict], accounts: list[dict], resid: pd.DataFrame, cfg: dict, min_days: int) -> list[dict]:
    """Pure: the accounts to open. Greedy per cohort by Sharpe low end (then name): an eligible series never
    shadowed joins while its cohort has a free slot and its residual returns correlate at |rho| max_rho or
    under with each active account and pick of that cohort. A pair with under min_days shared days counts as
    different."""
    ever = {tuple(a[k] for k in KEY) for a in accounts}
    rho = resid.corr(min_periods=min_days) if not resid.empty else pd.DataFrame()
    held = {c: [tuple(a[k] for k in KEY) for a in accounts if a["cohort"] == c and a["state"] != "retired"]
            for c in ("short", "long")}
    out = []
    for c in sorted(candidates, key=lambda c: (-(c["sharpe_lo"] or -np.inf), *(c[k] for k in KEY))):
        key = tuple(c[k] for k in KEY)
        if key in ever or not eligible(c, cfg):
            continue
        cohort = cohort_of(c["median_hold_hours"], cfg)
        mine = held[cohort]
        if len(mine) >= cfg[cohort]["max_active"]:
            continue
        if key in rho.columns and any(k in rho.columns and abs(rho.loc[key, k]) > cfg["max_rho"] for k in mine):
            continue
        mine.append(key)
        ever.add(key)
        out.append({"account": name_of(*key), **dict(zip(KEY, key)), "cohort": cohort,
                    "capital": float(cfg["capital"]),
                    "entry": {k: c[k] for k in ("sharpe_lo", "expectancy_lo", "trades", "median_hold_hours",
                                                "oos_trades", "oos_expectancy")}})
    return out


def marks(start: dt.date, equity: float, through: dt.date, daily: dict[dt.date, float]) -> list[tuple]:
    """Pure: (day, ret, equity) for each day after `start` up to `through`, a day without a trade closing at 0."""
    out, day = [], start + dt.timedelta(days=1)
    while day <= through:
        ret = float(daily.get(day, 0.0))
        equity *= 1 + ret
        out.append((day, ret, equity))
        day += dt.timedelta(days=1)
    return out


def judge(account: str, state: str, capital: float, rets: list[float], equity: list[float], cfg: dict) -> tuple:
    """Pure: (state, why) by the written rule; why is None unless it retires."""
    c = cfg
    if not rets:
        return state, None
    eq = np.asarray(equity, dtype=float)
    drawdown = float(1 - (eq / np.maximum.accumulate(np.concatenate([[capital], eq]))[1:]).min())
    if drawdown > c["max_drawdown"]:
        return "retired", f"drawdown {drawdown:.1%} past {c['max_drawdown']:.0%}"
    if len(rets) < c["min_days"]:
        return state, None
    lo, hi = metrics.block_mean_ci(np.asarray(rets), metrics.seed_of(account, len(rets)), block=c["block"],
                                   level=c["level"])
    if hi is not None and hi < 0:
        return "retired", f"mean daily return interval below zero after {len(rets)} days"
    if eq[-1] > capital and lo is not None and lo > 0:
        return "winning", None
    if state == "winning":
        return "retired", "lost its edge: up and interval above zero no longer both hold"
    if len(rets) >= c["max_days"]:
        return "retired", f"{len(rets)} days without winning"
    return state, None


def residual_frame(conn, keys: list[tuple[str, str, str]]) -> pd.DataFrame:
    """Each series' BTC-residual daily returns, one column per (strategy, exchange, overlay)."""
    from lab import cohorts  # cohorts imports lab.analyze, which runs this step
    rows = conn.execute(f"SELECT strategy, exchange, overlay, day, ret FROM {DAILY} WHERE (strategy, exchange, overlay) "
                        "IN (SELECT * FROM unnest(%s::text[], %s::text[], %s::text[]))",
                        [[k[i] for k in keys] for i in range(3)]).fetchall() if keys else []
    return diversity.residuals(cohorts.returns_frame(rows, KEY), cohorts.btc_returns(conn))


def run(conn, cfg: dict | None = None, today: dt.date | None = None) -> dict:
    from lab.analyze import CORR_MIN_DAYS
    cfg = (cfg or suggest.load_config())["shadow"]
    today = today or dt.datetime.now(dt.timezone.utc).date()
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        accounts = cur.execute(ACCOUNTS).fetchall()
        through = {(s, e, o): d for s, e, o, d in conn.execute(THROUGH)}
        marked = retired = 0
        for a in (a for a in accounts if a["state"] != "retired"):
            key = tuple(a[k] for k in KEY)
            last = conn.execute("SELECT day, equity FROM shadow_marks WHERE account = %s ORDER BY day DESC LIMIT 1",
                                (a["account"],)).fetchone()
            start, eq = last if last else (a["promoted_on"], a["capital"])
            end = min(through.get(key, start), today - dt.timedelta(days=1))
            daily = dict(conn.execute(f"SELECT day, ret FROM {DAILY} WHERE strategy = %s AND exchange = %s "
                                      "AND overlay = %s AND day > %s AND day <= %s", (*key, start, end)).fetchall())
            new = marks(start, eq, end, daily)
            cur.executemany("INSERT INTO shadow_marks (account, day, ret, equity) VALUES (%s, %s, %s, %s)",
                            [(a["account"], *m) for m in new])
            marked += len(new)
            hist = conn.execute("SELECT ret, equity FROM shadow_marks WHERE account = %s ORDER BY day",
                                (a["account"],)).fetchall()
            state, why = judge(a["account"], a["state"], a["capital"], [h[0] for h in hist], [h[1] for h in hist],
                               {**cfg, **cfg[a["cohort"]]})
            if state != a["state"]:
                conn.execute("UPDATE shadow_accounts SET state = %s, retired_on = %s, retired_why = %s "
                             "WHERE account = %s", (state, today if why else None, why, a["account"]))
                a["state"] = state
                retired += state == "retired"
        candidates = cur.execute(CANDIDATES, (cfg["window"],)).fetchall()
        keys = sorted({tuple(c[k] for k in KEY) for c in candidates if eligible(c, cfg)}
                      | {tuple(a[k] for k in KEY) for a in accounts if a["state"] != "retired"})
        new = promote(candidates, accounts, residual_frame(conn, keys), cfg, CORR_MIN_DAYS)
        cur.executemany("INSERT INTO shadow_accounts (account, strategy, exchange, overlay, cohort, capital, promoted_on, "
                        "entry) VALUES (%(account)s, %(strategy)s, %(exchange)s, %(overlay)s, %(cohort)s, %(capital)s, "
                        "%(promoted_on)s, %(entry)s)", [dict(n, promoted_on=today, entry=Jsonb(n["entry"])) for n in new])
        counts = dict(((c, s), n) for c, s, n in conn.execute(
            "SELECT cohort, state, count(*) FROM shadow_accounts GROUP BY 1, 2"))
    active = lambda c: counts.get((c, "trial"), 0) + counts.get((c, "winning"), 0)  # noqa: E731
    return {"shadow_promoted": len(new), "shadow_retired": retired, "shadow_marked": marked,
            "shadow_active_short": active("short"), "shadow_active_long": active("long"),
            "shadow_winning": counts.get(("short", "winning"), 0) + counts.get(("long", "winning"), 0)}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"shadow: {run(conn)}", flush=True)
    sys.exit(0)
