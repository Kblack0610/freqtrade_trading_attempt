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
