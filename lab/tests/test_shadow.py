import datetime as dt

import numpy as np
import pandas as pd

from lab import shadow, suggest

CFG = suggest.load_config()["shadow"]
DAY = dt.date(2026, 11, 1)


def cand(strategy, exchange="binanceus", sharpe_lo=1.0, hold=4.0, **kw):
    return {"strategy": strategy, "exchange": exchange, "overlay": "", "sharpe_lo": sharpe_lo, "expectancy_lo": 0.001, "trades": 100,
            "median_hold_hours": hold, "oos_trades": 80, "oos_expectancy": 0.002, **kw}


def resid(series: dict) -> pd.DataFrame:
    days = pd.date_range("2025-11-01", periods=200, freq="D")
    return pd.DataFrame({k: v for k, v in series.items()}, index=days)


def test_promote_takes_only_gate_passing_uncorrelated_pairs_into_their_holding_cohort():
    rng = np.random.default_rng(1)
    a, b, c = rng.normal(0, 0.01, 200), rng.normal(0, 0.01, 200), rng.normal(0, 0.01, 200)
    frame = resid({("A", "binanceus", ""): a, ("Twin", "binanceus", ""): a + rng.normal(0, 0.001, 200),
                   ("B", "binanceus", ""): b, ("L", "binanceus", ""): c})
    cands = [cand("A", sharpe_lo=2.0), cand("Twin", sharpe_lo=1.5), cand("B"), cand("L", hold=48),
             cand("NoOOS", oos_expectancy=-0.001), cand("FewTrades", trades=5), cand("NoHold", hold=None)]
    out = shadow.promote(cands, [], frame, CFG, 60)
    assert [(n["account"], n["cohort"]) for n in out] == [
        ("shadow-A-binanceus", "short"), ("shadow-B-binanceus", "short"), ("shadow-L-binanceus", "long")]
    assert out[0]["entry"]["oos_expectancy"] == 0.002


def test_promote_fills_only_free_slots_and_never_reopens_a_retired_pair():
    cfg = {**CFG, "short": {**CFG["short"], "max_active": 2}}
    held = [{"strategy": "Old", "exchange": "binanceus", "overlay": "", "cohort": "short", "state": "trial"},
            {"strategy": "Gone", "exchange": "binanceus", "overlay": "", "cohort": "short", "state": "retired"}]
    out = shadow.promote([cand("Gone", sharpe_lo=9), cand("X", sharpe_lo=2), cand("Y")], held, pd.DataFrame(), cfg, 60)
    assert [n["strategy"] for n in out] == ["X"]


def test_an_overlay_is_its_own_series_keyed_by_its_id_and_held_to_the_same_diversity_bar():
    rng = np.random.default_rng(2)
    a, b = rng.normal(0, 0.01, 200), rng.normal(0, 0.01, 200)
    frame = resid({("A", "binanceus", ""): a, ("A", "binanceus", "o1"): a + rng.normal(0, 0.001, 200),
                   ("A", "binanceus", "o2"): b})
    held = [{"strategy": "A", "exchange": "binanceus", "overlay": "", "cohort": "short", "state": "trial"},
            {"strategy": "Gone", "exchange": "binanceus", "overlay": "", "cohort": "short", "state": "retired"}]
    cands = [cand("A", overlay="o1", sharpe_lo=3.0), cand("A", overlay="o2"), cand("Gone", overlay="o3"),
             cand("Gone", sharpe_lo=9)]
    out = shadow.promote(cands, held, frame, CFG, 60)
    # o1 moves with the open base account and is skipped; a retired base does not retire its overlays
    assert [(n["account"], n["overlay"]) for n in out] == [("shadow-A-binanceus-o2", "o2"),
                                                          ("shadow-Gone-binanceus-o3", "o3")]


def test_marks_append_days_after_the_start_compounding_and_zero_on_quiet_days():
    got = shadow.marks(DAY, 1000.0, DAY + dt.timedelta(days=3), {DAY: 0.5, DAY + dt.timedelta(days=1): 0.1,
                                                                 DAY + dt.timedelta(days=3): -0.1})
    assert [(d.day, r) for d, r, _ in got] == [(2, 0.1), (3, 0.0), (4, -0.1)]
    assert round(got[-1][2], 6) == round(1000 * 1.1 * 0.9, 6)
    assert shadow.marks(DAY, 1000.0, DAY, {DAY: 0.5}) == []


def rule(**kw):
    return {**CFG, **CFG["short"], **kw}


def curve(rets, capital=1000.0):
    return list(capital * np.cumprod(1 + np.asarray(rets)))


def test_judge_retires_on_drawdown_any_day():
    rets = [0.01, -0.25]
    assert shadow.judge("a", "trial", 1000.0, rets, curve(rets), rule())[0] == "retired"


def test_judge_keeps_a_young_account_in_trial_and_names_a_steady_winner():
    up = [0.004 + 0.001 * (i % 3) for i in range(40)]
    assert shadow.judge("a", "trial", 1000.0, up[:10], curve(up[:10]), rule()) == ("trial", None)
    assert shadow.judge("a", "trial", 1000.0, up, curve(up), rule()) == ("winning", None)


def test_judge_retires_a_loser_and_one_that_runs_out_of_days():
    down = [-0.004 - 0.001 * (i % 3) for i in range(35)]
    state, why = shadow.judge("a", "trial", 1000.0, down, curve(down), rule(max_drawdown=0.9))
    assert state == "retired" and "below zero" in why
    flat = [0.01 * (-1) ** i for i in range(60)]
    state, why = shadow.judge("a", "trial", 1000.0, flat, curve(flat), rule())
    assert state == "retired" and "without winning" in why
    state, why = shadow.judge("a", "winning", 1000.0, flat[:40], curve(flat[:40]), rule())
    assert state == "retired" and "lost its edge" in why


def seed(conn):
    conn.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades, sharpe_lo, expectancy_lo, "
                 "median_hold_hours) VALUES ('S', 'binanceus', '365d', 365, 100, 1.0, 0.001, 4), "
                 "('Bad', 'binanceus', '365d', 365, 100, 2.0, -0.001, 4)")
    conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, oos_expectancy, gates) "
                 "VALUES ('S', 'binanceus', 10, false, 1, 80, 0.002, '[]'), "
                 "('Bad', 'binanceus', 10, false, 1, 80, 0.002, '[]')")
    conn.execute("INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok) "
                 "VALUES ('S', 'binanceus', '2026-11-01', '2026-11-06', 'k', true)")


def test_run_promotes_then_marks_only_new_days_and_never_rewrites_a_mark(conn):
    seed(conn)
    first = shadow.run(conn, today=DAY)
    assert first["shadow_promoted"] == 1 and first["shadow_active_short"] == 1
    assert conn.execute("SELECT account, promoted_on FROM shadow_accounts").fetchall() == [("shadow-S-binanceus", DAY)]
    conn.execute("INSERT INTO strategy_daily (strategy, exchange, day, ret, pnl, trades) VALUES "
                 "('S', 'binanceus', '2026-11-01', 0.5, 0, 1), ('S', 'binanceus', '2026-11-02', 0.01, 0, 1), "
                 "('S', 'binanceus', '2026-11-04', 0.02, 0, 1)")
    second = shadow.run(conn, today=dt.date(2026, 11, 4))
    assert second["shadow_promoted"] == 0 and second["shadow_marked"] == 2  # the 2nd and 3rd; the 4th is today
    conn.execute("UPDATE strategy_daily SET ret = 0.9 WHERE day = '2026-11-02'")  # a backtest rerun moves a day
    third = shadow.run(conn, today=dt.date(2026, 11, 9))
    assert third["shadow_marked"] == 2  # the 4th and 5th: chunks cover through the 5th
    got = conn.execute("SELECT day, ret FROM shadow_marks ORDER BY day").fetchall()
    assert [(d.day, r) for d, r in got] == [(2, 0.01), (3, 0.0), (4, 0.02), (5, 0.0)]


def test_run_opens_an_overlay_account_and_marks_it_from_its_own_daily_returns(conn):
    seed(conn)
    conn.execute("INSERT INTO overlay_windows (strategy, exchange, overlay, win, days, trades, sharpe_lo, expectancy_lo, "
                 "median_hold_hours) VALUES ('S', 'binanceus', 'o1', '365d', 365, 100, 0.5, 0.001, 48)")
    conn.execute("INSERT INTO backtest_chunks (strategy, exchange, overlay, chunk_start, chunk_end, cache_key, ok) "
                 "VALUES ('S', 'binanceus', 'o1', '2026-11-01', '2026-11-04', 'k', true)")
    assert shadow.run(conn, today=DAY)["shadow_active_long"] == 1
    assert conn.execute("SELECT account, overlay, cohort FROM shadow_accounts WHERE overlay <> ''").fetchall() == \
        [("shadow-S-binanceus-o1", "o1", "long")]
    conn.execute("INSERT INTO strategy_daily (strategy, exchange, day, ret, pnl, trades) VALUES "
                 "('S', 'binanceus', '2026-11-02', 0.5, 0, 1)")
    conn.execute("INSERT INTO overlay_daily (strategy, exchange, overlay, day, ret, pnl, trades) VALUES "
                 "('S', 'binanceus', 'o1', '2026-11-02', 0.01, 0, 1), ('S', 'binanceus', 'o2', '2026-11-03', 0.9, 0, 1)")
    shadow.run(conn, today=dt.date(2026, 11, 9))
    # through the 3rd, where its own chunks end, and only its own overlay's returns
    got = conn.execute("SELECT day, ret FROM shadow_marks WHERE account = 'shadow-S-binanceus-o1' ORDER BY day").fetchall()
    assert [(d.day, r) for d, r in got] == [(2, 0.01), (3, 0.0)]
