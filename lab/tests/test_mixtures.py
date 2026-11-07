import datetime as dt

import numpy as np
import pandas as pd
import pytest

from lab import mixtures, scorecard, suggest

CFG = suggest.load_config()
C = CFG["mixtures"]


def month_list(n=6, start="2024-01-01"):
    firsts = pd.date_range(start, periods=n + 1, freq="MS")
    return [(a.date(), b.date()) for a, b in zip(firsts[:-1], firsts[1:])]


def sleeves(n=6, seed=3):
    """Three sleeves over n months: A and B unrelated, C quiet (a tenth of A's spread)."""
    days = pd.date_range("2024-01-01", month_list(n)[-1][1] - dt.timedelta(days=1), freq="D")
    rng = np.random.default_rng(seed)
    return {("A", "x"): pd.Series(rng.normal(0.001, 0.02, len(days)), index=days),
            ("B", "x"): pd.Series(rng.normal(0.001, 0.02, len(days)), index=days),
            ("C", "x"): pd.Series(rng.normal(0.0005, 0.002, len(days)), index=days)}


def test_a_weight_from_the_month_it_sizes_is_refused():
    daily = sleeves()
    frame = pd.DataFrame({f"{s} {e}": r for (s, e), r in daily.items()})
    start = pd.Timestamp("2024-03-01")
    spec = {"sleeves": [["A", "x"], ["B", "x"]], "weighting": "inverse_vol", "gate": False}
    with pytest.raises(ValueError, match="on or after"):
        mixtures.fit(frame[frame.index <= start], pd.Series(dtype=object), start, spec, C)
    w, _ = mixtures.fit(frame[frame.index < start], pd.Series(dtype=object), start, spec, C)
    assert w.sum() == pytest.approx(1.0)


def test_a_month_is_scored_with_weights_from_the_months_before_it():
    """C is quiet until March then wild: inverse vol still gives it the most in March, since it was fit on Jan-Feb."""
    daily = sleeves(3)
    c = daily[("C", "x")].copy()
    c[c.index >= "2024-03-01"] *= 50
    daily[("C", "x")] = c
    frame = pd.DataFrame({f"{s} {e}": r for (s, e), r in daily.items()})
    ret, w = mixtures.walk(frame, month_list(3), pd.Series(dtype=object),
                           {"sleeves": [list(k) for k in daily], "weighting": "inverse_vol", "gate": False}, C)
    assert w.argmax() == 2 and ret.index.min() == pd.Timestamp("2024-03-01") and len(ret) == 31


def test_risk_weights_favour_the_quiet_sleeve_and_skip_a_flat_one():
    daily = sleeves()
    frame = pd.DataFrame({f"{s} {e}": r for (s, e), r in daily.items()})
    for name in ("inverse_vol", "hrp"):
        w = mixtures.WEIGHTS[name](frame)
        assert w.sum() == pytest.approx(1.0) and w[2] > w[0] and w[2] > w[1], name
        flat = mixtures.WEIGHTS[name](frame.assign(**{"D x": 0.0}))
        assert flat[-1] == 0.0 and flat.sum() == pytest.approx(1.0), name


def test_the_gate_switches_a_sleeve_off_where_it_lost_in_training():
    days = pd.date_range("2024-01-01", "2024-03-31", freq="D")
    label = pd.Series(np.where(np.arange(len(days)) % 2 == 0, "up/low", "down/high"), index=days, dtype=object)
    a = pd.Series(np.where(label == "up/low", 0.01, -0.01), index=days)
    frame = pd.DataFrame({"A x": a, "B x": 0.001 + 0 * a})
    spec = {"sleeves": [["A", "x"], ["B", "x"]], "weighting": "equal", "gate": True}
    ret, _ = mixtures.walk(frame, month_list(3), label, spec, {**C, "min_train_months": 2})
    march = label[label.index >= "2024-03-01"]
    assert np.allclose(ret[march == "down/high"], 0.0005) and np.allclose(ret[march == "up/low"], 0.0055)
    ungated, _ = mixtures.walk(frame, month_list(3), label, {**spec, "gate": False}, {**C, "min_train_months": 2})
    assert ret.sum() > ungated.sum()


def test_specs_put_the_baseline_first_and_stop_at_the_budget():
    pool = [("A", "x"), ("B", "x"), ("C", "x"), ("D", "x")]
    out = mixtures.specs(pool, C)
    assert out[0] == {"sleeves": [["A", "x"], ["B", "x"], ["C", "x"]], "weighting": "equal", "gate": False}
    assert len({mixtures.variant_of(s) for s in out}) == len(out) == min(C["budget"], 1 + (6 + 4) * 3 * 2 - 1)
    assert len(mixtures.specs(pool, {**C, "budget": 5})) == 5 and mixtures.specs(pool[:1], C) == []


def test_more_trials_raise_the_bar():
    ret = sleeves()[("A", "x")] + 0.002
    one, many = mixtures.score(ret, 2, C, "k"), mixtures.score(ret, 500, C, "k")
    assert one["sharpe"] == many["sharpe"] and many["dsr"] < one["dsr"]


def test_scorecard_splits_a_mixture_by_climate():
    days = pd.date_range("2023-01-01", periods=400, freq="D")
    close = pd.Series(100 * 1.004 ** np.arange(400), index=days)
    vol = pd.Series("low", index=days, dtype=object)
    ret = pd.Series(0.001, index=days[250:])
    out = scorecard.build(close, vol, {}, {}, CFG, 3, {("1/N: A x, B x", "v"): ret})
    mix = [r for r in out if r["kind"] == "mixture"]
    assert [(r["name"], r["cell"], r["days"]) for r in mix] == [("1/N: A x, B x", "up/low", 150)]


def test_run_counts_each_mixture_as_a_trial_once(conn):
    daily = sleeves()
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('A', 'a', 'h'), ('B', 'b', 'h'), ('C', 'c', 'h')")
    for (s, e), r in daily.items():
        for a, b in month_list():
            conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, params) "
                         "VALUES (%s, %s, %s, %s, %s, 'k', true, '{}')", (s, e, a, a, b))
        for day, x in r.items():
            conn.execute("INSERT INTO walkforward_trades (strategy, exchange, variant, fold_start, pair, open_date, close_date, "
                         "profit_ratio) VALUES (%s, %s, 'tuned', %s, 'BTC/USD', %s, %s, %s)",
                         (s, e, day.date().replace(day=1), day, day + pd.Timedelta(hours=12), float(x)))
    for d in pd.date_range("2023-01-01", "2024-07-31", freq="D"):
        conn.execute("INSERT INTO regimes (day, btc_close, vol_bucket) VALUES (%s, 100, 'low')", (d.date(),))
    conn.execute("DELETE FROM trials WHERE strategy = 'mixture'")  # the register is not truncated between tests
    before = conn.execute("SELECT count(*) FROM trials").fetchone()[0]
    out = mixtures.run(conn, CFG)
    n = len(mixtures.specs([("A", "x"), ("B", "x"), ("C", "x")], C))
    assert out["mixtures"] == n == 1 + 4 * 6 - 1
    assert conn.execute("SELECT count(*) FROM trials").fetchone()[0] == before + n
    assert mixtures.run(conn, CFG) == out and conn.execute("SELECT count(*) FROM trials").fetchone()[0] == before + n
    base = conn.execute("SELECT months, days, trials, weights FROM mixtures WHERE baseline").fetchall()
    assert len(base) == 1 and base[0][:3] == (4, 122, before + n) and set(base[0][3]) == {"A x", "B x", "C x"}
    assert conn.execute("SELECT count(*) FROM mixture_daily").fetchone()[0] == 122 * n
