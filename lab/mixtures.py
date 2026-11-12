#!/usr/bin/env python3
"""Mixtures of strategies, each scored out of sample and counted as a trial.

A mixture is (sleeves, weighting, regime gate). A sleeve is one strategy on one exchange, its daily out-of-sample
return the walk-forward test months' trades (`walkforward_daily`, variant [mixtures] variant). Weighting is 1/N
(the baseline, DeMiguel, Garlappi and Uppal 2009), inverse volatility or hierarchical risk parity (Lopez de Prado
2016). The gate turns a sleeve on only on days whose climate (lab/scorecard.labels: BTC trend x volatility tercile,
read the day before) showed a positive mean for it in training; a sleeve that is off holds cash, nothing is moved
to the others. For each month the sleeves share, weights and gates are fit on the months before it and scored on
it: fit() refuses a training day on or after the month it sizes. So no mixture is built from in-sample backtests.

Every mixture is a `trials` row (strategy 'mixture', variant the md5 of its spec), so the deflated Sharpe bar
rises with each one, and the probability of backtest overfitting is computed across the set. The 1/N mixture of
the best-scored pool members, ungated, is the baseline the others are read against. Spec space and budget live
in suggest.toml [mixtures]. Advice for a human; nothing here trades.

Run alone with `python -m lab.mixtures`; lab/analyze.py and lab/walkforward.py run it after the book.
"""

import itertools
import sys

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform

from lab import metrics, scorecard, suggest
from lab.db import connect
from lab.grid import variant_of

COLS = ["variant", "name", "spec", "weighting", "gated", "baseline", "sleeves", "months", "days", "annual", "lo", "hi",
        "sharpe", "max_drawdown", "dsr", "trials", "pbo", "weights"]
MONTHS = """
SELECT strategy, exchange, fold_start, test_end FROM walkforward
WHERE ok AND (params IS NOT NULL OR %(variant)s = 'base') AND test_end <= %(lockbox)s ORDER BY 1, 2, 3
"""
DAILY = "SELECT strategy, exchange, day, ret FROM walkforward_daily WHERE variant = %(variant)s AND day < %(lockbox)s"
ORDER = "SELECT strategy, exchange FROM confidence ORDER BY ready DESC, score DESC NULLS LAST, strategy, exchange"
LABELS = {"equal": "1/N", "inverse_vol": "inverse vol", "hrp": "HRP"}


def name_of(spec: dict) -> str:
    sleeves = ", ".join(f"{s} {e}" for s, e in spec["sleeves"])
    return f"{LABELS[spec['weighting']]}: {sleeves}" + (" (regime gate)" if spec["gate"] else "")


def inverse_vol(train: pd.DataFrame) -> np.ndarray:
    """1 / sd per sleeve. A sleeve flat through training has no estimate and gets nothing."""
    sd = train.std(ddof=1).to_numpy(dtype=float)
    w = np.divide(1.0, sd, out=np.zeros_like(sd), where=sd > 0)
    return w / w.sum() if w.sum() else w


def cluster_var(cov: np.ndarray, idx: list[int]) -> float:
    c = cov[np.ix_(idx, idx)]
    w = 1.0 / np.diag(c)
    w /= w.sum()
    return float(w @ c @ w)


def hrp(train: pd.DataFrame) -> np.ndarray:
    """Hierarchical risk parity: single-linkage tree on correlation distance, then recursive bisection of the
    quasi-diagonal order, splitting each pair of clusters by inverse cluster variance. Flat sleeves get nothing."""
    sd = train.std(ddof=1).to_numpy(dtype=float)
    live = np.flatnonzero(sd > 0)
    w = np.zeros(train.shape[1])
    if len(live) < 2:
        w[live] = 1.0
        return w
    x = train.iloc[:, live]
    cov, corr = x.cov().to_numpy(), np.clip(x.corr().to_numpy(), -1.0, 1.0)
    dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, 1.0))
    np.fill_diagonal(dist, 0.0)
    order = [int(i) for i in leaves_list(linkage(squareform(dist, checks=False), "single"))]
    part = np.ones(len(live))
    clusters = [order]
    while clusters:
        clusters = [c[a:b] for c in clusters for a, b in ((0, len(c) // 2), (len(c) // 2, len(c))) if len(c) > 1]
        for left, right in zip(clusters[::2], clusters[1::2]):
            vl, vr = cluster_var(cov, left), cluster_var(cov, right)
            alpha = 1.0 - vl / (vl + vr)
            part[left] *= alpha
            part[right] *= 1.0 - alpha
    w[live] = part / part.sum()
    return w


WEIGHTS = {"equal": lambda train: np.full(train.shape[1], 1.0 / train.shape[1]), "inverse_vol": inverse_vol, "hrp": hrp}


def fit(train: pd.DataFrame, label: pd.Series, test_start: pd.Timestamp, spec: dict, cfg: dict) -> tuple[np.ndarray, dict]:
    """Weights per sleeve and, when gated, the climates each sleeve trades in, from days before test_start only."""
    if len(train) and train.index.max() >= test_start:
        raise ValueError(f"training reaches {train.index.max().date()}, on or after the month it sizes ({test_start.date()})")
    w = WEIGHTS[spec["weighting"]](train)
    if not spec["gate"]:
        return w, {}
    on = {}
    for col in train.columns:
        both = pd.concat([train[col].rename("ret"), label.rename("cell")], axis=1, join="inner")
        by = both.groupby("cell")["ret"].agg(["mean", "count"])
        on[col] = set(by[(by["count"] >= cfg["gate_min_days"]) & (by["mean"] > 0)].index)
    return w, on


def walk(frame: pd.DataFrame, months: list[tuple], label: pd.Series, spec: dict, cfg: dict) -> tuple[pd.Series, np.ndarray]:
    """frame: daily returns by sleeve over `months` (fold_start, test_end). Each month after the first
    min_train_months is sized on the months before it. The mixture's daily return and the last month's weights."""
    out, w = [], np.zeros(frame.shape[1])
    for start, end in months[cfg["min_train_months"]:]:
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        w, on = fit(frame[frame.index < start], label, start, spec, cfg)
        test = frame[(frame.index >= start) & (frame.index < end)]
        if on:
            cell = label.reindex(test.index)
            mask = np.column_stack([cell.isin(on[c]).to_numpy() for c in frame.columns])
            out.append(pd.Series((test.to_numpy() * mask) @ w, index=test.index))
        else:
            out.append(pd.Series(test.to_numpy() @ w, index=test.index))
    return (pd.concat(out) if out else pd.Series(dtype=float)), w


def specs(pool: list[tuple], cfg: dict) -> list[dict]:
    """The baseline first, then every sleeve set of 2 to max_sleeves from the pool by each weighting and gate, in a
    fixed order, cut at the budget so the same pool gives the same trials."""
    base = {"sleeves": [list(p) for p in pool[:cfg["max_sleeves"]]], "weighting": "equal", "gate": False}
    out = [base] if len(pool) >= 2 else []
    for k in range(2, cfg["max_sleeves"] + 1):
        for sleeves in itertools.combinations(pool, k):
            for weighting in cfg["weightings"]:
                for gate in cfg["gates"]:
                    s = {"sleeves": [list(p) for p in sleeves], "weighting": weighting, "gate": gate}
                    if s != base:
                        out.append(s)
    return out[:cfg["budget"]]


def score(ret: pd.Series, trials: int, cfg: dict, key: str) -> dict:
    x = ret.to_numpy(dtype=float)
    lo, hi = metrics.block_mean_ci(x, metrics.seed_of("mixture", key), block=cfg["block"], level=cfg["level"]) \
        if len(x) >= 2 else (None, None)
    sd = float(x.std(ddof=1)) if len(x) > 1 else 0.0
    sr = float(x.mean() / sd) if sd else None
    dsr = metrics.deflated_sharpe(sr, len(x), float(ret.skew()), float(ret.kurt()) + 3.0, trials) if sr is not None else None
    return {"days": len(x), "annual": float(x.mean() * metrics.YEAR) if len(x) else None,
            "lo": None if lo is None else lo * metrics.YEAR, "hi": None if hi is None else hi * metrics.YEAR,
            "sharpe": None if sr is None else sr * np.sqrt(metrics.YEAR), "dsr": dsr,
            "max_drawdown": metrics.max_drawdown(x) if len(x) else None}


def frame_of(spec: dict, months: dict, daily: dict) -> tuple[pd.DataFrame, list[tuple]]:
    """The sleeves' daily returns over the months all of them were tested, zero on a day without a trade."""
    keys = [tuple(p) for p in spec["sleeves"]]
    shared = sorted(set.intersection(*(set(months[k]) for k in keys)))
    days = pd.DatetimeIndex([d for a, b in shared for d in pd.date_range(a, pd.Timestamp(b) - pd.Timedelta(days=1))])
    cols = {f"{s} {e}": daily.get((s, e), pd.Series(dtype=float)).reindex(days).fillna(0.0) for s, e in keys}
    return pd.DataFrame(cols, index=days), shared


def build(months: dict, daily: dict, order: list[tuple], label: pd.Series, cfg: dict) -> list[tuple]:
    """months: (strategy, exchange) -> [(fold_start, test_end)]; daily: (strategy, exchange) -> daily return Series.
    Each spec with at least one month scored: the spec, its returns, its last weights by sleeve, whether it is the
    baseline."""
    c = cfg["mixtures"]
    ranked = [p for p in order if p in months] + sorted(p for p in months if p not in order)
    pool = [p for p in ranked if len(months[p]) > c["min_train_months"]][:c["pool"]]
    out = []
    for i, spec in enumerate(specs(pool, c)):
        frame, shared = frame_of(spec, months, daily)
        if len(shared) <= c["min_train_months"]:
            continue
        ret, w = walk(frame, shared, label, spec, c)
        out.append((spec, ret, dict(zip(frame.columns, map(float, w))), i == 0))
    return out


def set_pbo(rets: list[pd.Series], blocks: int) -> float | None:
    """PBO across the mixtures (metrics.pbo_panel over the median mixture's history); None under its minimum days."""
    res = metrics.pbo_panel(dict(enumerate(rets)), blocks)
    return None if res is None else res["pbo"]


def rows(built: list[tuple], trials: int, cfg: dict) -> list[dict]:
    c = cfg["mixtures"]
    pbo = set_pbo([b[1] for b in built], c["pbo_blocks"])
    out = []
    for spec, ret, w, baseline in built:
        v = variant_of(spec)
        out.append({"variant": v, "name": name_of(spec), "spec": Jsonb(spec), "weighting": spec["weighting"],
                    "gated": spec["gate"], "baseline": baseline,
                    "sleeves": len(spec["sleeves"]), "months": len(ret.index.to_period("M").unique()),
                    **score(ret, trials, c, v), "trials": trials, "pbo": pbo, "weights": Jsonb(w)})
    return out


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    q = {"variant": cfg["mixtures"]["variant"], "lockbox": cfg["lockbox"]["start"]}
    months: dict = {}
    for s, e, a, b in conn.execute(MONTHS, q):
        months.setdefault((s, e), []).append((a, b))
    daily: dict = {}
    for s, e, day, ret in conn.execute(DAILY, q):
        daily.setdefault((s, e), {})[pd.Timestamp(day)] = ret
    daily = {k: pd.Series(v, dtype=float) for k, v in daily.items()}
    label = scorecard.labels(scorecard.regimes(conn))
    built = build(months, daily, [tuple(p) for p in conn.execute(ORDER)], label, cfg)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO trials (strategy, exchange, variant) VALUES ('mixture', '', %s) ON CONFLICT DO NOTHING",
                            [(variant_of(b[0]),) for b in built])
        trials = conn.execute("SELECT count(*) FROM trials").fetchone()[0]
        out = rows(built, trials, cfg)
        conn.execute("DELETE FROM mixtures")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO mixtures ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
            cur.executemany("INSERT INTO mixture_daily (variant, day, ret) VALUES (%s, %s, %s)",
                            [(variant_of(b[0]), d.date(), float(x)) for b in built for d, x in b[1].items()])
    return {"mixtures": len(out), "mixtures_clearing": sum(r["lo"] is not None and r["lo"] > 0 for r in out)}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"mixtures: {run(conn)}", flush=True)
    sys.exit(0)
