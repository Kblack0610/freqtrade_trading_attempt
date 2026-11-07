import datetime as dt

import numpy as np
import pandas as pd

from lab import confidence, metrics, suggest

CFG = suggest.load_config()
C = CFG["confidence"]
NOW = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
PAIR = ("Good", "binanceus")


def folds(returns, months=6):
    return [{"tuned_returns": list(part), "tuned_trades": len(part), "tuned_expectancy": float(np.mean(part))}
            for part in np.array_split(returns, months)]


def population(rng, noise, planted=0, edge=0.2, trades=400):
    wf = {(f"noise{i}", "x"): folds(rng.normal(0, 1, trades)) for i in range(noise)}
    wf.update({(f"edge{i}", "x"): folds(rng.normal(edge, 1, trades)) for i in range(planted)})
    return wf


def days(n=1500):
    return [dt.date(2022, 1, 1) + dt.timedelta(days=i) for i in range(n)]


def good_facts():
    """A pair that clears every gate: an edge in every BTC trend, a population of others around it."""
    rng = np.random.default_rng(1)
    d = days()
    close = pd.Series(100 * np.exp(0.12 * np.sin(np.arange(len(d)) / 60)) + 100, index=d)
    daily = pd.Series(rng.normal(0.004, 0.01, len(d)), index=d)
    wf = {PAIR: folds(rng.normal(0.02, 0.03, 300))}
    wf[("Good", "okx")] = folds(rng.normal(0.02, 0.03, 300))
    wf.update({(f"o{i}", "binanceus"): folds(rng.normal(0.001, 0.03, 300)) for i in range(24)})
    w = {"trials": 10.0, "trials_raw": 20, "start_day": d[0], "end_day": d[-1], "hold_excess_lo": 0.05,
         "worst_open": -0.12, "fee_per_trade": 0.0014}
    return {"strategies": {"Good": {"lookahead": False, "recursive": False}},
            "series": {PAIR: {"windows": {"all": w}}}, "walkforward": wf, "daily": {PAIR: daily}, "close": close,
            "trend": metrics.regimes(close, C["trend_band"], 90).set_index("day")["trend"],
            "overfit": {("binanceus", "all"): 0.1}, "pairs": {PAIR: [(100, 2.0), (100, 1.5), (100, 1.0)]}, "drift": {PAIR: {"gap": 0.0005, "trades": 40}},
            "live": {PAIR: {"since": dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc), "closed": 80, "stopouts": 3}},
            "live_returns": {PAIR: list(np.random.default_rng(2).normal(0.02, 0.03, 80))}, "roster": {PAIR: ["bot-good"]},
            "now": NOW}


def result(facts, trials=30):
    return next(r for r in confidence.rows(facts, trials, CFG) if (r["strategy"], r["exchange"]) == PAIR)


def failed(row):
    return [g["gate"] for g in row["gates"].obj if not g["pass"]]


def test_pure_noise_clears_the_score_and_interval_under_five_percent_of_the_time():
    rng = np.random.default_rng(7)
    passed = total = 0
    for _ in range(4):
        ev = confidence.evidence(population(rng, 100), C)
        score = confidence.scores(ev, C)
        passed += sum(score[p] >= C["ready_score"] and ev[p]["lo"] > 0 for p in ev)
        total += len(ev)
    assert passed / total < 0.05


def test_a_market_lifting_every_strategy_does_not_lift_the_score():
    wf = population(np.random.default_rng(3), 60, edge=0.0)
    lifted = {p: [{**f, "tuned_returns": [r + 0.05 for r in f["tuned_returns"]]} for f in v] for p, v in wf.items()}
    score = confidence.scores(confidence.evidence(lifted, C), C)
    assert sum(s >= C["ready_score"] for s in score.values()) / len(score) < 0.05


def test_a_planted_edge_scores_ready_among_noise():
    ev = confidence.evidence(population(np.random.default_rng(11), 95, planted=5, edge=0.3), C)
    score = confidence.scores(ev, C)
    assert all(score[f"edge{i}", "x"] >= C["ready_score"] and ev[f"edge{i}", "x"]["lo"] > 0 for i in range(5))
    assert np.median([s for p, s in score.items() if p[0].startswith("noise")]) < C["ready_score"]


def test_no_score_without_a_population():
    ev = confidence.evidence(population(np.random.default_rng(5), C["min_population"] - 1), C)
    assert confidence.scores(ev, C) == {}


def test_every_gate_passing_is_ready():
    row = result(good_facts())
    assert failed(row) == [] and row["ready"] and row["passed"] == len(row["gates"].obj) == 19
    assert row["oos_trades"] == 300


def spoil(facts, name):
    w = facts["series"][PAIR]["windows"]["all"]
    if name == "bias":
        facts["strategies"]["Good"]["recursive"] = None
    elif name == "costs":
        w["fee_per_trade"] = 0.004
    elif name == "overfit probability":
        facts["overfit"][("binanceus", "all")] = 0.3
    elif name == "excess over hold, low":
        w["hold_excess_lo"] = -0.01
    elif name == "worst open loss":
        w["worst_open"] = -0.75
    elif name == "drift":
        facts["drift"][PAIR]["gap"] = -0.05
    elif name == "dry run":
        facts["live"][PAIR]["closed"] = 59
    elif name == "not decayed":
        facts["live_returns"][PAIR] = list(np.random.default_rng(3).normal(-0.02, 0.03, 80))
    return facts


def test_one_bad_gate_is_named_and_blocks_ready_whatever_the_score():
    for name in ("bias", "costs", "overfit probability", "excess over hold, low", "worst open loss", "drift", "dry run",
                 "not decayed"):
        row = result(spoil(good_facts(), name))
        assert failed(row) == [name] and not row["ready"] and row["score"] >= C["ready_score"]


def test_a_bull_only_strategy_fails_the_regime_spread():
    facts = good_facts()
    up = facts["trend"] == "up"
    facts["daily"][PAIR] = facts["daily"][PAIR].where(up.reindex(facts["daily"][PAIR].index, fill_value=False), 0.0)
    assert "regime spread" in failed(result(facts))


def test_an_edge_smaller_than_slippage_fails_only_that_room():
    facts = good_facts()
    facts["walkforward"][PAIR] = folds(np.tile([0.0005, 0.0025], 150))
    assert "slippage room" in failed(result(facts))


def test_one_pair_carrying_the_profit_fails_the_holdout():
    facts = good_facts()
    facts["pairs"][PAIR] = [(50, 9.0), (100, -1.0), (100, -0.5)]
    assert "without its best pair" in failed(result(facts))
    assert confidence.without_best_pair([(10, 1.0)]) is None


def test_one_venue_alone_fails_agreement():
    facts = good_facts()
    del facts["walkforward"][("Good", "okx")]
    assert "venues agreeing" in failed(result(facts))
    facts["walkforward"][("Good", "okx")] = folds(np.random.default_rng(2).normal(-0.02, 0.03, 300))
    assert "venues agreeing" in failed(result(facts))


def test_a_large_loss_run_fails_the_reorder_at_stage_one_stake():
    facts = good_facts()
    facts["walkforward"][PAIR] = folds(np.tile([0.6, -0.5], 150))
    assert "trades reordered, worst fall" in failed(result(facts))
    assert metrics.reorder_drawdown(np.full(50, 0.01), 1) == 0


def test_more_trials_raise_the_bar():
    low = next(g for g in result(good_facts(), trials=30)["gates"].obj if g["gate"].startswith("deflated"))
    facts = good_facts()
    facts["daily"][PAIR] = facts["daily"][PAIR] * 0.0 + np.random.default_rng(2).normal(0.0006, 0.01, 1500)
    thin = [next(g for g in result(facts, trials=n)["gates"].obj if g["gate"].startswith("deflated")) for n in (30, 3000)]
    assert low["pass"] and thin[0]["value"] > thin[1]["value"]


def test_a_dry_run_needs_a_real_btc_fall():
    facts = good_facts()
    facts["close"] = pd.Series(np.linspace(100, 200, 1500), index=days())
    facts["live"][PAIR]["since"] = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)
    assert "dry run" in failed(result(facts))


def edge(n=300, mean=0.02, sd=0.03, seed=5):
    return confidence.interval(np.random.default_rng(seed).normal(mean, sd, n), 1, C)


def test_live_trades_at_the_true_edge_do_not_decay_and_at_the_low_end_rarely_do():
    ev = edge()
    sd = ev["returns"].std()
    at_edge = [confidence.decay(np.random.default_rng(i).normal(0.02, sd, 1000), ev, C)["at"] for i in range(40)]
    assert at_edge == [None] * 40
    at_low = [confidence.decay(np.random.default_rng(i).normal(ev["lo"], sd, 1000), ev, C)["at"] for i in range(40)]
    assert sum(a is not None for a in at_low) <= 12  # Lorden: one alarm in 1000 trades on average, so far under 63% in 1000


def test_a_sustained_loss_run_decays_but_one_bad_week_does_not():
    ev = edge()
    good = list(np.random.default_rng(6).normal(0.02, 0.03, 200))
    week = [-0.03] * 5
    assert confidence.decay(good + week + good, ev, C)["at"] is None
    d = confidence.decay(good + [-0.02] * 120, ev, C)
    assert d["at"] is not None and 200 < d["at"] < 320


def test_decay_waits_for_a_trade_and_an_edge_to_decay_from():
    ev = edge()
    assert confidence.decay([], ev, C) is None
    assert confidence.decay([-0.05] * 50, edge(mean=0.0), C) is None
    assert confidence.decay([-0.05] * 50, edge(n=10), C) is None
    assert confidence.decay_gate(None, C)["pass"]


def test_old_good_trades_bank_nothing_against_a_later_decay():
    ev = edge()
    bad = [-0.02] * 120
    alone = confidence.decay(bad, ev, C)["at"]
    after = confidence.decay([0.05] * 2000 + bad, ev, C)["at"]
    assert alone is not None and after == alone + 2000


def test_decay_retires_only_roster_bots_and_leaves_the_hard_stop_alone():
    facts = good_facts()
    assert confidence.retire_rows(facts, CFG) == []
    facts["live_returns"][PAIR] = list(np.random.default_rng(3).normal(-0.02, 0.03, 200))
    [r] = confidence.retire_rows(facts, CFG)
    assert (r["kind"], r["strategy"], r["exchange"], r["bot"]) == ("retire", "Good", "binanceus", "bot-good")
    assert "not decayed" in failed(result(facts)) and "trades reordered, worst fall" not in failed(result(facts))
    facts["roster"] = {}
    assert confidence.retire_rows(facts, CFG) == []


def test_a_decayed_bot_is_retired_through_the_database_and_cleared_when_it_recovers(conn):
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('S', 'x', 'h')")
    for i, part in enumerate(np.array_split(np.random.default_rng(1).normal(0.02, 0.03, 300), 6)):
        conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, "
                     "tuned_trades, tuned_expectancy, tuned_returns) VALUES ('S', 'binanceus', %s, %s, %s, 'k', true, %s, %s, %s)",
                     (dt.date(2023, 1 + i, 1), dt.date(2022, 1, 1), dt.date(2023, 2 + i, 1), len(part), part.mean(),
                      list(part)))
    conn.execute("INSERT INTO bots (name, strategy, exchange) VALUES ('bot-s', 'S', 'binanceus')")

    def trades(returns):
        conn.execute("DELETE FROM bot_trades")
        for i, r in enumerate(returns):
            day = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(hours=i)
            conn.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, close_date, profit_ratio, raw) "
                         "VALUES ('bot-s', %s, %s, 'BTC/USDT', false, %s, %s, '{}')", (i, day, day, r))

    def retires():
        return conn.execute("SELECT bot FROM suggestions WHERE kind = 'retire' AND exchange <> ''").fetchall()

    trades([0.02] * 150 + [-0.02] * 150)
    assert confidence.run(conn)["decayed"] == 1 and retires() == [("bot-s",)]
    suggest.run(conn)
    assert retires() == [("bot-s",)]
    trades([0.02] * 150)
    assert confidence.run(conn)["decayed"] == 0 and retires() == []
    gate = conn.execute("SELECT gates FROM confidence").fetchone()[0]
    assert next(g for g in gate if g["gate"] == "not decayed")["pass"]


def test_the_decay_retire_survives_suggest_and_goes_when_the_decay_does(conn):
    keep = dict(kind="retire", strategy="S", exchange="binanceus", bot="b", score=None, summary="s", action="a", gates=[])
    gone = dict(kind="retire", strategy="L", exchange="", bot="", score=None, summary="s", action="a", gates=[])
    suggest.store(conn, [keep])
    suggest.store(conn, [gone])
    assert conn.execute("SELECT strategy FROM suggestions ORDER BY 1").fetchall() == [("L",), ("S",)]
    suggest.store(conn, [])
    assert conn.execute("SELECT strategy FROM suggestions").fetchall() == [("S",)]
    suggest.store(conn, [], suggest.DECAY)
    assert conn.execute("SELECT count(*) FROM suggestions").fetchone()[0] == 0


def test_no_walkforward_is_not_ready_and_says_so():
    facts = good_facts()
    facts["walkforward"] = {}
    row = result(facts)
    assert row["score"] is None and not row["ready"] and row["oos_trades"] == 0
    assert {"out-of-sample trades", "expectancy interval low", "months positive", "score"} <= set(failed(row))


def test_the_register_keeps_a_strategy_that_is_gone(conn):
    conn.execute("TRUNCATE trials")
    conn.execute("INSERT INTO strategies (name, file, file_hash, load_error) VALUES ('Gone', 'g.py', 'h', 'no import')")
    assert confidence.register(conn) == 1
    conn.execute("DELETE FROM strategies")
    assert confidence.register(conn) == 1


def test_run_stores_a_row_and_replaces_the_last(conn):
    conn.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades) VALUES ('S', 'binanceus', 'all', 10, 3)")
    assert confidence.run(conn)["confidence"] == 1
    conn.execute("DELETE FROM strategy_windows")
    assert confidence.run(conn)["confidence"] == 0
    assert conn.execute("SELECT count(*) FROM confidence").fetchone()[0] == 0


def test_nothing_inside_the_lockbox_reaches_the_score(conn):
    cfg = {**suggest.load_config(), "lockbox": {"start": dt.date(2026, 5, 1)}}
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('S', 'x', 'h')")
    for start, end in ((dt.date(2026, 4, 1), dt.date(2026, 5, 1)), (dt.date(2026, 5, 1), dt.date(2026, 6, 1))):
        conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok) "
                     "VALUES ('S', 'kraken', %s, %s, %s, 'k', true)", (start, start, end))
    for day in (dt.date(2026, 4, 30), dt.date(2026, 5, 1)):
        conn.execute("INSERT INTO strategy_daily (strategy, exchange, day, ret, pnl, trades) "
                     "VALUES ('S', 'kraken', %s, 0.01, 1, 1)", (day,))
    chunk = conn.execute("INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok) "
                         "VALUES ('S', 'kraken', '2026-01-01', '2026-12-31', 'k', true) RETURNING id").fetchone()[0]
    for day in ("2026-04-30", "2026-05-01"):
        conn.execute("INSERT INTO backtest_trades (chunk_id, pair, open_date, close_date, profit_ratio, profit_abs) "
                     "VALUES (%s, %s, %s, %s, 0.01, 1)", (chunk, f"P{day}", day, day))
    facts = confidence.load(conn, cfg)
    key = ("S", "kraken")
    assert [f["fold_start"] for f in facts["walkforward"][key]] == [dt.date(2026, 4, 1)]
    assert list(facts["daily"][key].index) == [dt.date(2026, 4, 30)]
    assert len(facts["pairs"][key]) == 1


def test_evidence_and_scores_are_unchanged_by_the_returns_input():
    # Golden values from master before confidence.interval was split out of evidence (P5 of the markets plan).
    rng = np.random.default_rng(42)
    wf = {(f"S{i}", "okx"): [
        {"tuned_returns": list(rng.normal(0.002 * (i - 10), 0.03, 40)), "tuned_expectancy": 0.001, "tuned_trades": 40},
        {"tuned_returns": list(rng.normal(0.001, 0.03, 30)), "tuned_expectancy": -0.001, "tuned_trades": 30}]
        for i in range(22)}
    ev = confidence.evidence(wf, C)
    s = confidence.scores(ev, C)
    for k, mean, lo, hi, score in [
            ("S0", -0.009282064138291163, -0.01622398703761035, -0.0010675201060717546, 2.041151978047268),
            ("S15", 0.010890307166464469, 0.004794840497024354, 0.017003900255372965, 99.91366937280887),
            ("S21", 0.009201126807628404, -0.0028057990507848593, 0.018756305800261462, 89.6049698260757)]:
        e = ev[(k, "okx")]
        assert (e["mean"], e["lo"], e["hi"], s[(k, "okx")]) == (mean, lo, hi, score)
        assert e["trades"] == 70 and e["months"] == [0.001, -0.001]
