import importlib.util
import pathlib

import pytest

from lab import cohorts, league, variants

_spec = importlib.util.spec_from_file_location(
    "cohort_roster", pathlib.Path(__file__).resolve().parents[2] / "ci" / "cohort_roster.py")
cohort_roster = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cohort_roster)

CFG = {"prefix": "agg", "cohort": "aggressive", "top": 2, "rank_venues": ["binanceus"],
       "venues": ["binanceus", "kraken"]}
ROWS = [("A", "binanceus", "", 0.10), ("A", "binanceus", "ov1", 0.30), ("A", "binanceus", "gone", 0.90),
        ("B", "binanceus", "", 0.20), ("B", "binanceus", "ov1", 0.20),
        ("C", "binanceus", "", 0.05), ("D", "binanceus", "", 0.50), ("D", "coinbase", "", 0.90)]


def test_top_strategies_by_the_low_end_each_with_its_best_known_overlay_one_row_per_venue():
    lines = cohorts.pick(ROWS, CFG, {"ov1"})
    assert lines == ["agg-binanceus-d binanceus D aggressive base", "agg-kraken-d kraken D aggressive base",
                     "agg-binanceus-a binanceus A aggressive ov1", "agg-kraken-a kraken A aggressive ov1"]
    # an overlay the toml no longer declares is never picked; a tie goes to the base
    assert [line.split()[2] for line in cohorts.pick(ROWS, CFG, set())] == ["D", "D", "B", "B"]
    assert cohorts.pick(ROWS, {**CFG, "top": 3}, {"ov1"})[4].endswith("B aggressive base")


def test_a_strategy_needs_a_row_on_every_rank_venue():
    cfg = {**CFG, "rank_venues": ["binanceus", "coinbase"], "top": 5}
    assert {line.split()[2] for line in cohorts.pick(ROWS, cfg, set())} == {"D"}
    assert cohorts.pick([], CFG, set()) == []


def test_the_shipped_cohort_config_picks_rows_league_accepts(tmp_path):
    cfg = {"prefix": "agg", "cohort": "aggressive", **cohorts.load()["aggressive"]}
    known, _ = variants.load()
    oid = next(iter(known))
    rows = [(f"S{i}", cfg["rank_venues"][0], oid, 0.1 * i) for i in range(1, 9)]
    lines = cohorts.pick(rows, cfg, set(known))
    assert len(lines) == cfg["top"] * len(cfg["venues"])
    roster = tmp_path / "roster.tsv"
    roster.write_text("\n".join(lines))
    parsed = league.roster_rows(roster)
    assert {r.cohort for r in parsed} == {"aggressive"} and {r.overlay for r in parsed} == {oid}


MIX = {"prefix": "mix", "cohort": "mixture"}
MIXES = [("v1", [["A", "binanceus"], ["B", "coinbase"]], 0.02, 1.1),
         ("v2", [["C", "kraken"], ["A", "binanceus"], ["D", "binanceus"]], 0.05, 0.4),
         ("v3", [["E", "binanceus"], ["F", "binanceus"]], 0.05, 0.9)]


def test_a_mixture_cohort_is_the_best_mixtures_sleeves_each_on_its_own_exchange():
    assert cohorts.pick_mixture(MIXES, MIX) == ["mix-binanceus-e binanceus E mixture base",
                                                "mix-binanceus-f binanceus F mixture base"]
    assert cohorts.pick_mixture(MIXES[:2], MIX)[0] == "mix-kraken-c kraken C mixture base"
    assert cohorts.pick_mixture([("v4", [["G", "okx"]], 0.05, None)] + MIXES[:1], MIX) == ["mix-okx-g okx G mixture base"]
    assert cohorts.pick_mixture([], MIX) == []


def test_mixture_rows_are_only_the_runnable_weighting_and_gate_with_enough_days(conn):
    from psycopg.types.json import Jsonb
    for v, weighting, gated, days, lo in (("ok", "equal", False, 120, 0.01), ("hrp", "hrp", False, 120, 0.5),
                                         ("gate", "equal", True, 120, 0.5), ("short", "equal", False, 30, 0.5),
                                         ("nolo", "equal", False, 120, None)):
        conn.execute("INSERT INTO mixtures (variant, name, spec, weighting, gated, baseline, sleeves, months, days, lo, "
                     "trials, weights) VALUES (%s, %s, %s, %s, %s, false, 1, 4, %s, %s, 9, '{}')",
                     (v, v, Jsonb({"sleeves": [["A", "binanceus"]]}), weighting, gated, days, lo))
    cfg = {"prefix": "mix", "cohort": "mixture", **cohorts.load()["mixture"]}
    got = cohorts.mixture_rows(conn, cfg["weighting"], cfg["gated"], cfg["min_days"])
    assert got == [("ok", [["A", "binanceus"]], 0.01, None)]
    lines = cohorts.pick_mixture(got, cfg)
    assert lines == ["mix-binanceus-a binanceus A mixture base"]


def test_the_shipped_mixture_cohort_picks_rows_league_accepts(tmp_path):
    cfg = {"prefix": "mix", "cohort": "mixture", **cohorts.load()["mixture"]}
    roster = tmp_path / "roster.tsv"
    roster.write_text("\n".join(cohorts.pick_mixture(MIXES, cfg)))
    parsed = league.roster_rows(roster)
    assert {r.cohort for r in parsed} == {"mixture"} and {r.overlay for r in parsed} == {""}


ROSTER = "# bots\nbinanceus-s1 binanceus S1\nkraken-s1 kraken S1\n"
STS = "spec:\n  replicas: 2  # rows\n"
LINES = ["agg-binanceus-a binanceus A aggressive ov1", "agg-kraken-a kraken A aggressive ov1"]


def test_the_roster_pr_appends_every_row_and_replicas_follow():
    roster, sts = cohort_roster.seed(ROSTER, STS, "aggressive", LINES)
    assert roster == ROSTER + "\n".join(LINES) + "\n" and "replicas: 4  # rows" in sts


def test_a_cohort_already_in_the_roster_is_never_reseeded_or_resized():
    roster, sts = cohort_roster.seed(ROSTER, STS, "aggressive", LINES)
    assert cohort_roster.seed(roster, sts, "aggressive", LINES + ["agg-okx-b okx B aggressive base"]) is None
    assert cohort_roster.seed(ROSTER, STS, "aggressive", []) is None


def test_a_statefulset_out_of_step_with_the_roster_is_refused():
    with pytest.raises(ValueError, match="replicas 5 for 2 roster rows"):
        cohort_roster.seed(ROSTER, STS.replace("2  #", "5  #"), "aggressive", LINES)


def test_score_rows_leave_out_biased_and_unloadable_strategies(conn):
    for name, err in (("Good", None), ("Biased", None), ("Broken", "boom")):
        conn.execute("INSERT INTO strategies (name, file, file_hash, load_error) VALUES (%s, 'f', 'h', %s)", (name, err))
    conn.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias) VALUES ('Biased', 'lookahead', 'h', true)")
    cols = "(strategy, exchange, win, days, trades, sharpe_lo)"
    for name in ("Good", "Biased", "Broken"):
        conn.execute(f"INSERT INTO strategy_windows {cols} VALUES (%s, 'binanceus', '365d', 365, 40, 0.2)", (name,))
    conn.execute(f"INSERT INTO strategy_windows {cols} VALUES ('Good', 'binanceus', '90d', 90, 40, 0.2)")
    conn.execute(f"INSERT INTO overlay_windows (overlay, strategy, exchange, win, days, trades, sharpe_lo) "
                 "VALUES ('ov1', 'Good', 'binanceus', '365d', 365, 5, 0.9)")
    got = cohorts.score_rows(conn, "365d", ["binanceus"], 30)
    assert got == [("Good", "binanceus", "", 0.2)]
