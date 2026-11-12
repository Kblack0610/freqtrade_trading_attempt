import datetime as dt

import numpy as np
import pandas as pd

from lab import diversity

CFG = {"window": "365d", "min_trades": 30, "hold_hours": [1, 24, 168], "freq_buckets": 3, "max_rho": 0.5,
       "skip_families": ["skipped"]}


def row(strategy, exchange="binanceus", sharpe_lo=0.5, trades=60, days=365, hold=6.0):
    return {"strategy": strategy, "exchange": exchange, "sharpe_lo": sharpe_lo, "trades": trades, "days": days,
            "median_hold_hours": hold, "winrate": 0.5, "timeframe": "1h"}


def test_hold_buckets_cover_both_ends_and_a_missing_hold_is_none():
    names = diversity.hold_labels([1, 24, 168])
    assert names == ["under 1h", "1h to 24h", "24h to 168h", "over 168h"]
    assert [diversity.bucket(v, [1, 24, 168], names) for v in (0.5, 1, 23.9, 24, 200)] == \
        ["under 1h", "1h to 24h", "1h to 24h", "24h to 168h", "over 168h"]
    assert diversity.bucket(None, [1], ["a", "b"]) is None
    assert diversity.bucket(float("nan"), [1], ["a", "b"]) is None


def test_best_climate_is_the_widest_low_end_above_zero_and_none_when_none_clears():
    got = diversity.best_climates([
        {"name": "A", "exchange": "x", "cell": "up/low", "lo": 0.1},
        {"name": "A", "exchange": "x", "cell": "down/high", "lo": 0.3},
        {"name": "A", "exchange": "x", "cell": "flat/mid", "lo": None},
        {"name": "B", "exchange": "x", "cell": "up/low", "lo": -0.2}])
    assert got == {("A", "x"): "down/high", ("B", "x"): "none"}


def test_describe_drops_skipped_families_and_labels_untagged_and_untested():
    rows = [row("A", trades=12), row("B", trades=60, hold=None), row("C", trades=240, hold=200), row("D")]
    out = diversity.describe(rows, {"A": "trend", "B": "trend", "D": "skipped"}, {("A", "binanceus"): "up/low"}, CFG)
    by = {r["strategy"]: r for r in out}
    assert set(by) == {"A", "B", "C"}
    assert by["C"]["family"] == "untagged"
    assert by["A"]["climate"] == "up/low" and by["B"]["climate"] == "untested"
    assert by["B"]["hold"] == "unknown" and by["C"]["hold"] == "over 168h"
    assert [by[n]["freq"] for n in "ABC"] == ["low", "mid", "high"]


def test_too_few_strategies_for_frequency_buckets_share_one():
    out = diversity.describe([row("A")], {"A": "trend"}, {}, CFG)
    assert out[0]["freq"] == "all"


def test_each_cell_keeps_its_highest_low_end_and_a_projection_counts_the_gaps():
    rows = [row("A", sharpe_lo=0.2), row("B", sharpe_lo=0.9), row("C", hold=0.5, sharpe_lo=-1.0)]
    out = diversity.build(rows, [], {n: "trend" for n in "ABC"}, {**CFG, "freq_buckets": 1})
    assert len(out["cells"]) == 2
    assert {e["strategy"] for e in out["cells"].values()} == {"B", "C"}
    [trend] = out["by_hold"]
    assert [c["best"]["strategy"] if c["best"] else None for c in trend["by"]] == ["C", "B", None, None]
    assert trend["gaps"] == 2
    [by_climate] = out["by_climate"]
    assert by_climate["gaps"] == len(diversity.CLIMATES) - 1


def test_a_tie_on_low_end_picks_the_same_elite_every_time():
    rows = [row("B", sharpe_lo=0.5), row("A", sharpe_lo=0.5)]
    assert [e["strategy"] for e in diversity.elites(diversity.describe(rows, {}, {}, CFG)).values()] == ["B"]
    rows.reverse()
    assert [e["strategy"] for e in diversity.elites(diversity.describe(rows, {}, {}, CFG)).values()] == ["B"]


def test_shipped_config_loads():
    cfg = diversity.config()
    assert cfg["hold_hours"] == sorted(cfg["hold_hours"]) and 0 < cfg["max_rho"] < 1


def test_residuals_take_out_btc_by_its_beta_and_keep_what_is_left():
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(120)]
    rng = np.random.default_rng(3)
    btc = pd.Series(rng.normal(0, 0.03, len(days)), index=pd.to_datetime(days))
    own = rng.normal(0, 0.01, len(days))
    returns = pd.DataFrame({("S", "x"): 1.5 * btc.to_numpy() + own, ("Flat", "x"): own}, index=days)
    got = diversity.residuals(returns, btc)
    # Negative control: before, S tracks BTC; after, it is the series' own part and no longer does.
    assert returns[("S", "x")].corr(pd.Series(btc.to_numpy(), index=days)) > 0.9
    assert abs(got[("S", "x")].corr(btc)) < 0.1
    assert got[("S", "x")].corr(got[("Flat", "x")]) > 0.95
    assert diversity.residuals(pd.DataFrame(), btc).empty


def test_clusters_join_what_moves_together_and_keep_the_rest_apart():
    rng = np.random.default_rng(7)
    days = pd.date_range("2026-01-01", periods=200, freq="D")
    a, b = rng.normal(0, 0.01, len(days)), rng.normal(0, 0.01, len(days))
    resid = pd.DataFrame({("A1", "x"): a + rng.normal(0, 0.003, len(days)), ("A2", "x"): a + rng.normal(0, 0.003, len(days)),
                          ("A3", "x"): -a + rng.normal(0, 0.003, len(days)), ("B", "x"): b,
                          ("Short", "x"): pd.Series(a[:20], index=days[:20])}, index=days)
    got = diversity.clusters(resid, 0.5, 60)
    # A1, A2 and the mirror A3 are one bet (|rho| cut); B is its own; Short shares too few days to join anything.
    assert got[("A1", "x")] == got[("A2", "x")] == got[("A3", "x")] == 1
    assert len({got[("B", "x")], got[("Short", "x")], 1}) == 3
    # Negative control: with the cut at 0.99 nothing is close enough, so every series is alone.
    assert len(set(diversity.clusters(resid, 0.99, 60).values())) == 5
    assert diversity.clusters(resid[[("B", "x")]], 0.5, 60) == {("B", "x"): 1}


def test_cluster_table_shows_the_families_inside_each_shared_cluster():
    members = [{"strategy": s, "exchange": "x", "cluster": c} for s, c in (("T1", 1), ("M1", 1), ("T2", 2), ("T3", 2), ("Lone", 3))]
    members.append({"strategy": "T1", "exchange": "y", "cluster": 1})
    venues = [{"exchange": "y", "series": 1, "clusters": 1, "neff": 1.0}, {"exchange": "x", "series": 5, "clusters": 3, "neff": 2.7}]
    fam = {"T1": "trend", "T2": "trend", "T3": "trend", "M1": "mean-reversion"}
    x, y = diversity.cluster_table(members, venues, fam, {("M1", "x"): 0.4, ("T1", "x"): 0.1})
    assert y["exchange"] == "y" and y["groups"] == [] and y["singletons"] == 1
    assert [g["cluster"] for g in x["groups"]] == [1, 2] and x["singletons"] == 1 and x["mixed"] == 1
    assert x["groups"][0]["families"] == [("mean-reversion", 1), ("trend", 1)] and x["groups"][0]["best"] == "M1"
    assert x["groups"][1]["families"] == [("trend", 2)] and x["groups"][1]["best_lo"] is None
