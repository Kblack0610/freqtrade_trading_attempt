"""The stock gate (lab/stocks/gate.py) on hand-built windows: a variant failing one threshold is not ready and names it,
and only a variant passing every other gate reads the lockbox."""

import datetime as dt

import numpy as np
import pytest
from psycopg.types.json import Jsonb

from lab.core import readiness
from lab.core.grid import variant_of, variants
from lab.crypto import suggest
from lab.stocks import gate

START = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
BT = {"windows": 6, "window_days": 120, "lockbox_months": 4, "pbo_blocks": 4}
CCFG = {**suggest.load_config()["confidence"], "min_oos_trades": 100, "min_dsr": 0.0, "max_pbo": 1.0, "min_btl_ratio": 0.0,
        "min_folds": 4, "min_folds_positive": 0.5}


def window(i: int, mean: float, seed: int, trades: int = 40) -> dict:
    rng = np.random.default_rng(seed + i)
    returns = rng.normal(mean, 0.02, trades)
    daily = rng.normal(mean / 10, 0.005, BT["window_days"])
    return {"start_at": START + dt.timedelta(days=BT["window_days"] * i), "returns": returns.tolist(),
            "events": [f"d{k}" for k in range(trades)], "daily": daily.tolist()}


def variant(mean: float, seed: int, lockbox_mean: float | None = None, trades: int = 40):
    lock = mean if lockbox_mean is None else lockbox_mean
    return ([window(i, mean, seed, trades) for i in range(5)], [window(5, lock, seed, trades)])


def failed(r: dict) -> list[str]:
    return [g["gate"] for g in r["gates"] if not g["pass"]]


def test_a_variant_under_the_trade_floor_is_not_ready_and_never_reads_the_lockbox():
    out = gate.judge({"a": variant(0.02, 1, trades=10), "b": variant(0.02, 2)}, 10, CCFG, 4, "trend")
    assert not out["a"]["ready"] and "out-of-sample trades" in failed(out["a"])
    assert next(g for g in out["a"]["gates"] if g["gate"] == "lockbox")["value"] == "not read until the rest pass"


def test_a_variant_passing_every_other_gate_reads_the_lockbox_once_and_a_losing_one_fails_there():
    out = gate.judge({"a": variant(0.02, 1), "b": variant(0.02, 2, lockbox_mean=-0.05)}, 10, CCFG, 4, "trend")
    assert out["a"]["ready"] and failed(out["a"]) == []
    assert not out["b"]["ready"] and failed(out["b"]) == ["lockbox"]
    assert next(g for g in out["b"]["gates"] if g["gate"] == "lockbox")["value"] < 0


def test_the_newest_run_splits_into_test_windows_and_a_lockbox_of_the_newest_months():
    older = [{"start_at": START - dt.timedelta(days=7), "end_at": None}]
    run = [{"start_at": START + dt.timedelta(days=120 * i)} for i in range(6)]
    test, lock = gate.split(older + run, BT)
    assert [r["start_at"] for r in test] == [r["start_at"] for r in run[:5]]
    assert [r["start_at"] for r in lock] == [run[5]["start_at"]]
    assert gate.split(run, {**BT, "lockbox_months": 8})[1] == run[4:]
    assert gate.split([], BT) == ([], [])


def test_the_gate_writes_one_readiness_row_per_variant(conn):
    cfg = {"backtest": BT, "families": {"trend": {"sma": [50, 200]}}}
    names = [variant_of(p) for p in variants(cfg["families"]["trend"])]
    for v, seed in zip(names, (1, 2)):
        test, lock = variant(0.02, seed)
        for w in test + lock:
            conn.execute(
                "INSERT INTO market_backtests (family, venue, variant, params, start_at, end_at, orders, fills, trades, "
                "open_fills, metrics, returns, events, daily, trials) VALUES ('trend', 'alpaca', %s, %s, %s, %s, 0, 0, %s, 0, "
                "%s, %s, %s, %s, 0)", (v, Jsonb({}), w["start_at"], w["start_at"] + dt.timedelta(days=120), len(w["returns"]),
                                       Jsonb({}), w["returns"], w["events"], w["daily"]))
    out = gate.gate(conn, cfg, CCFG)
    rows = conn.execute("SELECT strategy, ready, windows FROM readiness WHERE market = 'stocks' ORDER BY strategy").fetchall()
    assert [r[0] for r in rows] == sorted(f"trend/{v}" for v in names) and {r[2] for r in rows} == {5}
    assert out == {"trend": sum(r[1] for r in rows)}
    assert readiness.STOCKS == "stocks"
