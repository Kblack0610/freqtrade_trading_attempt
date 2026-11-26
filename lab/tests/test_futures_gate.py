"""The futures gate (lab/futures/gate.py) on hand-built trades: each trade is net of its venue's recorded funding, so a
strategy ready before funding is not ready once a long pays it, and the gate writes one readiness row per strategy per venue."""

import datetime as dt

import numpy as np
import pandas as pd

from lab.core import readiness
from lab.crypto import suggest
from lab.futures import funding, gate, paper

START = pd.Timestamp("2024-01-01")
CFG = paper.load_config()
GCFG = CFG["gate"]
CCFG = {**suggest.load_config()["confidence"], "min_oos_trades": 100, "min_dsr": 0.0, "max_pbo": 1.0, "min_btl_ratio": 0.0,
        "min_folds": 4, "min_folds_positive": 0.5}
STAKE, WALLET = 100.0, 1000.0
PAIR = "BTC/USDT:USDT"


def trades(mean: float, seed: int, days: int = 360, short_every: int = 0) -> pd.DataFrame:
    """One one-day trade a day: gross return `mean` on its stake plus noise; every `short_every`-th is a short."""
    rng = np.random.default_rng(seed)
    opened = START + pd.to_timedelta(np.arange(days), unit="D")
    gross = rng.normal(mean, 0.01, days) * STAKE
    short = np.zeros(days, bool) if not short_every else np.arange(days) % short_every == 0
    return pd.DataFrame({"pair": PAIR, "open_date": opened, "close_date": opened + pd.Timedelta(hours=23),
                         "profit_abs": gross, "funding_fees": 0.0, "stake_amount": STAKE, "is_short": short})


def rates(rate: float, venue: str = "kucoin", days: int = 361) -> pd.DataFrame:
    """A settlement every 8 hours at `rate` from START (positive: longs pay)."""
    ts = START + pd.to_timedelta(np.arange(days * 3) * 8 - 4, unit="h")
    return pd.DataFrame({"exchange": venue, "pair": PAIR, "ts": ts, "rate": rate})


def judged(r: pd.DataFrame) -> dict:
    by = {f"s{i}": gate.windows(gate.net(trades(0.004, i), r, "kucoin"), GCFG["window_days"], WALLET) for i in range(3)}
    return gate.judge(by, 3, CCFG, GCFG, "kucoin")


def failed(v: dict) -> list[str]:
    return [g["gate"] for g in v["gates"] if not g["pass"]]


def test_a_futures_row_gets_gates_passed_and_ready():
    v = judged(rates(0.0))["s0"]
    assert v["ready"] and failed(v) == [] and v["passed"] == len(v["gates"])
    assert v["windows"] == 12 - GCFG["lockbox_windows"] and v["expectancy"] > 0


def test_a_row_that_fails_only_once_funding_is_subtracted_is_not_ready():
    # 0.002 three times a day is 0.006 of the stake per one-day long, more than its 0.004 gross edge
    before, after = judged(rates(0.0))["s0"], judged(rates(0.002))["s0"]
    assert before["ready"] and not after["ready"]
    assert "expectancy interval low, by event" in failed(after) and after["expectancy"] < 0


def test_funding_is_signed_by_side_and_a_venue_reads_only_its_own_series():
    t = trades(0.0, 1, days=4, short_every=2)
    n = gate.net(t, rates(0.001), "kucoin")
    assert np.allclose(n["net"] - n["gross"], np.where(t["is_short"], 1, -1) * STAKE * 0.003)
    assert gate.net(t, rates(0.001), "okx").empty
    late = rates(0.001).assign(ts=lambda r: r["ts"] + pd.Timedelta(days=2))
    assert len(gate.net(t, late, "kucoin")) == 2  # a trade opened before the series starts gets no funding guessed


def test_the_gate_writes_one_futures_row_per_strategy_per_venue(conn):
    conn.cursor().executemany("INSERT INTO funding_rates (exchange, pair, ts, rate) VALUES ('kucoin', %s, %s, 0)",
                              [(PAIR, t.to_pydatetime().replace(tzinfo=dt.timezone.utc)) for t in rates(0.0)["ts"]])
    out = gate.gate(conn, {s: trades(0.004, i) for i, s in enumerate(GCFG["strategies"])}, CFG, CCFG, WALLET)
    rows = conn.execute("SELECT venue, strategy, ready, windows FROM readiness WHERE market = %s ORDER BY 1, 2",
                        (readiness.FUTURES,)).fetchall()
    assert [(v, s) for v, s, _, _ in rows] == [(v, s) for v in sorted(funding.VENUES) for s in sorted(GCFG["strategies"])]
    assert out == {v: sum(r[2] for r in rows if r[0] == v) for v in funding.VENUES}
    assert not any(r[2] for r in rows if r[0] == "okx")  # no okx funding recorded, so no okx trade counts
    assert readiness.at_ready(conn, readiness.FUTURES, "kucoin", "DonchianLongShort") == next(
        r[2] for r in rows if r[:2] == ("kucoin", "DonchianLongShort"))
