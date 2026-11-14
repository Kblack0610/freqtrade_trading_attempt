import datetime as dt

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

from lab import metrics, shadow, suggest

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
    up = [0.006 + 0.001 * (i % 3) for i in range(40)]
    assert shadow.judge("a", "trial", 1000.0, up[:3], curve(up[:3]), rule()) == ("trial", None)
    assert shadow.judge("a", "trial", 1000.0, up, curve(up), rule()) == ("winning", None)


def test_judge_retires_a_loser_and_one_that_runs_out_of_days():
    down = [-0.006 - 0.001 * (i % 3) for i in range(35)]
    state, why = shadow.judge("a", "trial", 1000.0, down, curve(down), rule(max_drawdown=0.9))
    assert state == "retired" and "below zero" in why
    flat = [0.01 * (-1) ** i for i in range(60)]
    state, why = shadow.judge("a", "trial", 1000.0, flat, curve(flat), rule())
    assert state == "retired" and "without winning" in why
    state, why = shadow.judge("a", "winning", 1000.0, flat[:41], curve(flat[:41]), rule())
    assert state == "retired" and "lost its edge" in why


def null_run(rng, days: int, cfg: dict) -> str:
    """A zero-mean account judged every day, as the nightly step does, until it leaves trial."""
    rets, state = [], "trial"
    for _ in range(days):
        rets.append(float(np.clip(rng.normal(0, 0.01), -0.03, 0.03)))
        state, _ = shadow.judge("n", state, 1000.0, rets, curve(rets), cfg)
        if state != "trial":
            break
    return state


def test_a_null_account_wins_at_most_alpha_over_a_full_run():
    """The judge's error rate: zero-mean accounts, read every day to max_days, win no more often than alpha. The
    sequence's alpha holds at family 1 and, split over the slots, at family max_active (negative control below: the old
    rule, read the same way, breaks 10%)."""
    cfg = rule()
    rng = np.random.default_rng(7)
    runs = 150
    wins = sum(null_run(rng, cfg["max_days"], cfg) == "winning" for _ in range(runs))
    assert wins / runs <= cfg["alpha"]
    bonf = rule(family=cfg["max_active"])
    wins = sum(null_run(rng, bonf["max_days"], bonf) == "winning" for _ in range(runs))
    assert wins / runs <= bonf["alpha"] / bonf["family"] + 1 / runs


def test_the_old_bootstrap_rule_read_daily_breaks_alpha_where_the_sequence_does_not():
    """Negative control: the retired judge (a 90% block bootstrap re-read each day after 30 days) names a zero-mean account
    a winner far more often than 10% when read every day (optional stopping)."""
    rng = np.random.default_rng(11)
    runs, wins = 60, 0
    for r in range(runs):
        rets = rng.normal(0, 0.01, 60)
        for n in range(30, 61):
            lo, _ = metrics.block_mean_ci(rets[:n], r * 100 + n, samples=300, block=10, level=0.90)
            if lo is not None and lo > 0:
                wins += 1
                break
    assert wins / runs > 0.10


def test_a_strong_edge_wins_before_its_last_day_and_a_broken_one_retires_without_the_drawdown():
    cfg = rule()
    rng = np.random.default_rng(3)
    good = list(np.clip(rng.normal(0.01, 0.01, 60), -0.03, 0.03))
    for n in range(1, 61):
        if shadow.judge("g", "trial", 1000.0, good[:n], curve(good[:n]), cfg)[0] == "winning":
            break
    assert n < 30
    bad = list(np.clip(rng.normal(-0.008, 0.004, 60), -0.03, 0.03))
    state, why = shadow.judge("b", "trial", 1000.0, bad[:40], curve(bad[:40]), rule(max_drawdown=0.9))
    assert state == "retired" and "below zero" in why


def test_gate_funnel_counts_each_entry_gate_in_turn_and_promote_counts_what_it_turns_away():
    cands = [cand("A"), cand("B"), cand("FewTrades", trades=5), cand("NoOOS", oos_trades=3), cand("Neg", oos_expectancy=-1),
             cand("NoHold", hold=None)]
    f = shadow.gate_funnel(cands, CFG)
    assert f == {"series": 6, "has_hold": 5, "min_backtest_trades": 4, "expectancy_lo_above": 4, "min_oos_trades": 3,
                 "oos_expectancy_above": 2, "eligible": 2}
    old = {"account": "x", "strategy": "A", "exchange": "binanceus", "overlay": "", "cohort": "short", "state": "retired"}
    blocked: dict = {}
    out = shadow.promote(cands, [old], resid({}), {**CFG, "short": {**CFG["short"], "max_active": 1}}, 60, blocked)
    assert [o["account"] for o in out] == ["shadow-B-binanceus"] and blocked == {
        "shadowed_before": 1, "cohort_full": 0, "correlated": 0}
    blocked = {}
    shadow.promote(cands, [], resid({}), {**CFG, "short": {**CFG["short"], "max_active": 1}}, 60, blocked)
    assert blocked["cohort_full"] == 1


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


def test_funnel_reports_the_first_ok_night_the_exits_and_the_gate_holding_most(conn):
    gates = {"series": 100, "has_hold": 90, "min_backtest_trades": 80, "expectancy_lo_above": 30, "min_oos_trades": 20,
             "oos_expectancy_above": 10, "eligible": 10, "shadowed_before": 4, "cohort_full": 0, "correlated": 2,
             "admitted": 4}
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES "
                 "('analysis', 'bad', '{}', '2026-11-01', now(), false), ('analysis', 'a', '{}', '2026-11-02', now(), true), "
                 "('analysis', 'b', %s, '2026-11-03', now(), true)",
                 (Jsonb({"shadow_funnel": gates, "shadow_active_short": 8, "shadow_active_long": 60}),))
    for acct, state, promoted, retired in (("a", "winning", 1, None), ("b", "trial", 20, None), ("c", "retired", 1, 6),
                                           ("d", "retired", 1, 61)):
        conn.execute("INSERT INTO shadow_accounts (account, strategy, exchange, overlay, cohort, capital, promoted_on, "
                     "retired_on, state, entry) VALUES (%s, %s, 'binanceus', '', 'short', 1000, %s, %s, %s, '{}')",
                     (acct, acct, DAY + dt.timedelta(days=promoted), retired and DAY + dt.timedelta(days=retired), state))
    f = shadow.funnel(conn, suggest.load_config())
    assert (f["first_ok_night"], f["admitted_total"], f["winning"], f["trial"]) == ("2026-11-02", 4, 1, 1)
    assert (f["retired_early"], f["retired_at_max_days"]) == (1, 1)
    assert f["limit"] == "expectancy_lo_above" and f["free_slots"] == {"short": 52, "long": 0}
