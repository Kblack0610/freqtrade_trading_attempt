#!/usr/bin/env python3
"""The diversity archive (MAP-Elites): which ways of making money the backtests cover, and the best strategy of each.

docs/strategy-coverage.md, Diversity, defines it. Each lookahead-clean, loadable strategy x venue with enough trades
in `[diversity] window` (lab/suggest.toml) gets five descriptors: its family (lab/families.toml), the bucket of its
median holding time, the climate where its out-of-sample interval clears zero by the widest margin
(climate_scorecard; "none" when no climate clears, "untested" when it has no walk-forward), its venue, and the
bucket of its trades a month. Strategies sharing all five sit in one cell, and the cell's elite is the one with the
highest low end of the Sharpe interval. /coverage shows the cells; the diverse cohort picks from the elites, and
measures "different" on BTC-residual daily returns (`residuals`).
Nothing here writes: it reads the tables the nightly analysis fills.
"""

import math
import statistics
from pathlib import Path

import pandas as pd

from lab import cohorts, suggest

DAYS_A_MONTH = 365.25 / 12
AXES = ("family", "hold", "climate", "exchange", "freq")
CLIMATES = [f"{t}/{v}" for t in ("up", "flat", "down") for v in ("low", "mid", "high")] + ["none", "untested"]

ROWS = """
SELECT w.strategy, w.exchange, w.sharpe_lo, w.trades, w.days, w.median_hold_hours, w.winrate, s.timeframe
FROM strategy_windows w JOIN strategies s ON s.name = w.strategy AND s.load_error IS NULL
WHERE w.win = %s AND w.trades >= %s AND w.sharpe_lo IS NOT NULL
AND s.seen_at = (SELECT max(seen_at) FROM strategies)
AND NOT EXISTS (SELECT 1 FROM bias_checks b WHERE b.strategy = w.strategy AND b.kind = 'lookahead' AND b.has_bias)
"""
CLIMATE_ROWS = "SELECT name, exchange, cell, lo FROM climate_scorecard WHERE kind = 'strategy'"


def config(path: Path | None = None) -> dict:
    cfg = (suggest.load_config(path) if path else suggest.load_config())["diversity"]
    return {**cfg, "skip_families": cohorts.load()["strategies"]["skip_families"]}


def hold_labels(edges: list[float]) -> list[str]:
    """Bucket names from the edges in hours: [1, 24, 168] is under 1h, 1h to 24h, 24h to 168h, over 168h."""
    names = [f"under {edges[0]:g}h"] + [f"{a:g}h to {b:g}h" for a, b in zip(edges, edges[1:])]
    return names + [f"over {edges[-1]:g}h"]


def bucket(value: float | None, edges: list[float], names: list[str]) -> str | None:
    """The name of the bucket `value` falls in: below edges[0] is names[0], at or above edges[-1] is the last."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return names[sum(value >= e for e in edges)]


def freq_edges(per_month: list[float], n: int) -> list[float]:
    """Edges cutting the trades-a-month values into n equal-count buckets (fewer when there are too few values)."""
    if len(per_month) < n:
        return []
    return statistics.quantiles(per_month, n=n, method="inclusive")


def best_climates(climates: list[dict]) -> dict[tuple[str, str], str]:
    """(strategy, exchange) -> the climate whose interval low end clears zero by the most, any variant, or "none"."""
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for r in climates:
        key = (r["name"], r["exchange"])
        cur = out.get(key, (0.0, "none"))
        if r["lo"] is not None and r["lo"] > cur[0]:
            cur = (r["lo"], r["cell"])
        out[key] = cur
    return {k: v[1] for k, v in out.items()}


def describe(rows: list[dict], family: dict[str, str], climates: dict[tuple[str, str], str], cfg: dict) -> list[dict]:
    """Pure: each row with its five descriptors and trades a month. Rows of a skipped family are left out; an
    untagged strategy is family "untagged", so drift shows."""
    keep = [r for r in rows if family.get(r["strategy"], "untagged") not in cfg["skip_families"]]
    for r in keep:
        r["per_month"] = r["trades"] / r["days"] * DAYS_A_MONTH if r["days"] else 0.0
    edges = freq_edges([r["per_month"] for r in keep], cfg["freq_buckets"])
    freq_names = ["low", "mid", "high"] if len(edges) == 2 else [f"q{i + 1}" for i in range(len(edges) + 1)]
    hold_names = hold_labels(cfg["hold_hours"])
    return [{**r, "family": family.get(r["strategy"], "untagged"),
             "hold": bucket(r["median_hold_hours"], cfg["hold_hours"], hold_names) or "unknown",
             "climate": climates.get((r["strategy"], r["exchange"]), "untested"),
             "freq": bucket(r["per_month"], edges, freq_names) if edges else "all"} for r in keep]


def elites(described: list[dict]) -> dict[tuple, dict]:
    """Cell (the five descriptors) -> its elite: the highest Sharpe low end, then the name, so a tie is stable."""
    out: dict[tuple, dict] = {}
    for r in described:
        key = tuple(r[a] for a in AXES)
        if key not in out or (r["sharpe_lo"], r["strategy"]) > (out[key]["sharpe_lo"], out[key]["strategy"]):
            out[key] = r
    return out


def projection(cells: dict[tuple, dict], families: list[str], axis: str, values: list[str]) -> list[dict]:
    """Family x one axis: per pair, how many cells are filled and the best elite among them. A pair with no cell
    is a gap: nothing in that family has been backtested with that behavior."""
    i = AXES.index(axis)
    out = []
    for f in families:
        row = []
        for v in values:
            mine = [e for k, e in cells.items() if k[0] == f and k[i] == v]
            row.append({"value": v, "cells": len(mine), "best": max(mine, key=lambda e: e["sharpe_lo"], default=None)})
        out.append({"family": f, "by": row, "gaps": sum(not c["cells"] for c in row)})
    return out


def build(rows: list[dict], climates: list[dict], family: dict[str, str], cfg: dict) -> dict:
    """Pure: the archive as /coverage shows it, from the ROWS and CLIMATE_ROWS results: the cells, and family x
    holding period and family x climate."""
    described = describe([dict(r) for r in rows], family, best_climates(climates), cfg)
    cells = elites(described)
    fams = sorted({r["family"] for r in described})
    holds = hold_labels(cfg["hold_hours"]) + (["unknown"] if any(r["hold"] == "unknown" for r in described) else [])
    return {"cells": cells, "strategies": len(described), "window": cfg["window"],
            "by_hold": projection(cells, fams, "hold", holds), "holds": holds,
            "by_climate": projection(cells, fams, "climate", CLIMATES), "climates": CLIMATES}


def residuals(returns: pd.DataFrame, btc: pd.Series) -> pd.DataFrame:
    """Pure: each column of daily returns less beta x BTC's daily return, beta by least squares over the days they
    share (0 when under two). BTC is the one return source most strategies share (on 2026-11-09 the first principal
    component of the binanceus 365-day matrix was 28% of the variance at rho 0.84 with BTC), so what is left is what
    each strategy adds. A day BTC has no return keeps the strategy's own."""
    if returns.empty:
        return returns
    btc = btc.set_axis(pd.to_datetime(btc.index))
    out = {}
    for col in returns.columns:
        r = returns[col].dropna()
        r = r.set_axis(pd.to_datetime(r.index))
        both = pd.concat([r, btc], axis=1, join="inner").dropna()
        var = both.iloc[:, 1].var() if len(both) > 1 else 0.0
        beta = both.iloc[:, 0].cov(both.iloc[:, 1]) / var if var > 0 else 0.0
        out[col] = r - beta * btc.reindex(r.index).fillna(0.0)
    return pd.DataFrame(out)
