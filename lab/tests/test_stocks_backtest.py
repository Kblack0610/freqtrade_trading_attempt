"""The stock walk-forward (lab/stocks/backtest.py) on synthetic daily closes: a known trade, its fees, and no lookahead."""

import datetime as dt
from datetime import date as D

import pytest

from lab.inputs import calls
from lab.stocks import backtest, paper

SESSIONS = [D(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10)]
CLOSES = {"SPY": list(zip(SESSIONS, [10.0, 11.0, 12.0, 13.0, 9.0, 8.0, 8.0]))}
BPS, SETTLE = 2, 2


def cfg(**families):
    return {"universe": ["SPY"], "paper": {"capital": 10000.0, "stake": 1000.0, "max_open": 8},
            "families": families or {"trend": {"sma": 3}}}


def tape():
    return backtest.Tape(CLOSES, SETTLE)


def test_a_trend_variant_trades_a_known_sequence_with_its_fees():
    res = backtest.walk(paper.Trend({"sma": 3}), tape(), cfg(), BPS, SESSIONS[0], SESSIONS[-1] + dt.timedelta(days=1))
    shares = 1000 / 13                      # bought on the 4th session's close: the 3 closes before it average 11, 12 > 11
    entry_fee, exit_fee = 1000 * BPS / 1e4, shares * 8 * BPS / 1e4   # sold at the 6th session's close, 9 < avg(12, 13, 9)
    profit = shares * (8 - 13) - entry_fee - exit_fee
    assert len(res.trades) == 1 and res.orders == 1 and res.open_fills == 0
    t = res.trades.iloc[0]
    assert t["open_date"] == calls.close_at(D(2026, 11, 5)) and t["close_date"] == calls.close_at(D(2026, 11, 9))
    assert (t["open_rate"], t["close_rate"]) == (13.0, 8.0)
    assert t["profit_abs"] == pytest.approx(profit) and t["profit_ratio"] == pytest.approx(profit / (1000 + entry_fee))


def test_a_position_open_at_the_window_end_is_not_a_trade():
    res = backtest.walk(paper.Trend({"sma": 3}), tape(), cfg(), BPS, SESSIONS[0], D(2026, 11, 6))
    assert len(res.trades) == 0 and res.open_fills == 1


def test_a_bar_after_the_decision_time_raises_lookahead():
    t = tape()
    at = calls.close_at(D(2026, 11, 4))
    with pytest.raises(backtest.Lookahead):
        t.read("SPY", D(2026, 11, 5), at)
    assert t.read("SPY", D(2026, 11, 4), at) == 12.0


def test_the_closes_a_decision_sees_stop_at_the_last_final_bar():
    at = calls.close_at(D(2026, 11, 5)) - dt.timedelta(seconds=1)
    assert [d for d, _ in tape().final("SPY", at)] == SESSIONS[:3]


def test_windows_end_today_and_do_not_overlap():
    w = backtest.windows(D(2026, 11, 25), 3, 120)
    assert w[-1][1] == D(2026, 11, 25) and all(a[1] == b[0] for a, b in zip(w, w[1:])) and len(w) == 3


class Data:
    def closes(self, symbols, start, end=None):
        return {s: [(d, c) for d, c in CLOSES.get(s, [])] for s in symbols}


def test_a_run_stores_one_row_per_variant_and_window(conn):
    c = cfg(trend={"sma": [2, 3]}, reversion={"lookback": 2, "drop": 0.05, "exit_sma": 2, "max_days": 3})
    c["backtest"] = {"windows": 2, "window_days": 4, "warmup_days": 10}
    out = backtest.run(conn, Data(), c, D(2026, 11, 11), {"settle_hours": SETTLE, "cost_bps": BPS},
                       {"expectancy_level": 0.9})
    n = conn.execute("SELECT count(*) FROM market_backtests WHERE venue = 'alpaca'").fetchone()[0]
    assert len(out) == n == 3 * 2
