import datetime as dt
import importlib.util
import pathlib

import numpy as np
import pandas as pd
import pytest

from lab import analyze, cohorts, diversity, league, suggest, variants

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


ALL = {"prefix": "str", "cohort": "strategies", "venues": ["binanceus"], "skip_families": ["template"],
       "budget": {"binanceus": 3}}


def test_an_all_cohort_is_every_strategy_outside_the_skipped_families_one_row_per_venue():
    rows = [("B", "5m"), ("A", "1h"), ("T", "5m"), ("U", "1d")]
    lines = cohorts.pick_all(rows, ALL, {"A": "trend", "B": "grid", "T": "template"}, {"binanceus": 10})
    # an untagged strategy runs: only a family named in skip_families keeps a strategy out
    assert lines == ["str-binanceus-a binanceus A strategies base", "str-binanceus-b binanceus B strategies base",
                     "str-binanceus-u binanceus U strategies base"]
    assert cohorts.calls_per_minute("5m", 10) == 2 and cohorts.calls_per_minute("1h", 30) == 0.5


def test_an_all_cohort_over_its_venue_budget_is_refused_not_trimmed():
    with pytest.raises(ValueError, match="2 bots on binanceus make 20 candle calls a minute, over its budget of 3"):
        cohorts.pick_all([("A", "1m"), ("B", "1m")], ALL, {}, {"binanceus": 10})


def test_the_shipped_strategies_cohort_fits_its_budget_with_every_strategy_on_disk(tmp_path):
    """Every strategy file's own timeframe, as the nightly discovery stores it: a new 1m strategy that pushes the
    cohort over its venue budget fails here, before the picker refuses it in the cluster."""
    import re
    cfg = {"prefix": "str", "cohort": "strategies", **cohorts.load()["strategies"]}
    root = pathlib.Path(__file__).resolve().parents[2] / "user_data" / "strategies"
    rows = []
    for f in root.rglob("*.py"):
        src = f.read_text()
        tf = re.search(r"^\s+timeframe\s*=\s*['\"](\w+)['\"]", src, re.M)
        rows += [(name, tf[1] if tf else "5m") for name in re.findall(r"^class (\w+)\(", src, re.M)]
    family = cohorts.families()
    lines = cohorts.pick_all(rows, cfg, family, {v: len(analyze.pair_list(v)) for v in cfg["venues"]})
    assert len(lines) == len([s for s, _ in rows if family.get(s) not in cfg["skip_families"]]) * len(cfg["venues"])
    roster = tmp_path / "roster.tsv"
    roster.write_text("\n".join(lines))
    assert {r.cohort for r in league.roster_rows(roster)} == {"strategies"}


def test_strategy_rows_are_the_newest_discovery_less_biased_and_unloadable(conn):
    for name, err, seen in (("Good", None, "2026-11-09"), ("Biased", None, "2026-11-09"),
                            ("Broken", "boom", "2026-11-09"), ("Gone", None, "2026-11-01")):
        conn.execute("INSERT INTO strategies (name, file, file_hash, timeframe, load_error, seen_at) "
                     "VALUES (%s, 'f', 'h', '5m', %s, %s)", (name, err, seen))
    conn.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias) VALUES ('Biased', 'lookahead', 'h', true)")
    assert cohorts.strategy_rows(conn) == [("Good", "5m")]


def test_the_roster_pr_skips_a_row_a_bot_already_runs():
    lines = ["str-binanceus-s1 binanceus S1 strategies base", "str-kraken-s1 kraken S1 strategies ov1",
             "str-binanceus-s2 binanceus S2 strategies base"]
    roster, sts = cohort_roster.seed(ROSTER, STS, "strategies", lines)
    assert roster == ROSTER + "\n".join(lines[1:]) + "\n" and "replicas: 4  # rows" in sts


def test_two_cohorts_sharing_a_roster_prefix_are_refused_and_the_shipped_ones_are_distinct():
    with pytest.raises(ValueError, match="'strategies' and 'strategic' share the roster prefix 'str'"):
        cohorts.prefixes({"strategies": {}, "strategic": {}})
    shipped = cohorts.load()
    assert cohorts.prefixes(shipped) == {name: name[:3] for name in shipped}


DIV = {"prefix": "div", "cohort": "diverse", "venues": ["binanceus"], "replicate_on": ["okx", "kucoin"],
       "max_rho": 0.5, "max_bots": 16, "budget": {"binanceus": 10}}


def elite(name, lo, tf="1h", exchange="binanceus"):
    return {"strategy": name, "exchange": exchange, "sharpe_lo": lo, "timeframe": tf}


def edged(*names, on=("binanceus", "okx")):
    return {(n, v) for n in names for v in on}


def test_a_diverse_pick_cuts_on_btc_residuals_and_stops_at_max_bots():
    rng = np.random.default_rng(7)
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(200)]
    btc = pd.Series(rng.normal(0, 0.03, len(days)), index=days)
    src = {k: rng.normal(0, 0.01, len(days)) for k in "abcd"}
    # Copy shares A's source; B, C and D are their own. Every series also carries 0.8 x BTC, which alone makes them
    # all correlate: the cut must see through it.
    names = {"A": "a", "Copy": "a", "B": "b", "C": "c", "D": "d"}
    rows = [(n, "binanceus", d, float(src[k][i] + 0.8 * btc.iloc[i])) for n, k in names.items() for i, d in enumerate(days)]
    raw = cohorts.returns_frame(rows)
    resid = diversity.residuals(raw, btc)
    elites = [elite("A", 0.9), elite("Copy", 0.8), elite("B", 0.5), elite("C", 0.3), elite("D", 0.1)]
    ok = edged(*names)
    lines = cohorts.pick_archive(elites, resid, ok, DIV, {"binanceus": 10})
    assert [l.split()[2] for l in lines] == ["A", "B", "C", "D"]
    assert lines[0] == "div-binanceus-a binanceus A diverse base"
    assert len(cohorts.pick_archive(elites, resid, ok, {**DIV, "max_bots": 2}, {"binanceus": 10})) == 2
    # Negative controls: with the cut lifted the copy comes back; on raw returns the market makes all one.
    assert "Copy" in " ".join(cohorts.pick_archive(elites, resid, ok, {**DIV, "max_rho": 1.0}, {"binanceus": 10}))
    assert [l.split()[2] for l in cohorts.pick_archive(elites, raw, ok, DIV, {"binanceus": 10})] == ["A"]
    report = cohorts.neff_lift(elites, resid, lines)
    assert report.startswith("4 picks of 5 elites: residual effective count ") and report.endswith("(report only)")


def test_a_diverse_pick_needs_an_edge_before_fees_on_its_own_venue_and_a_second_one():
    elites = [elite("Both", 0.9), elite("OnlyHere", 0.8), elite("OnlyThere", 0.7), elite("Kucoin", 0.6)]
    ok = edged("Both") | {("OnlyHere", "binanceus"), ("OnlyThere", "okx"), ("Kucoin", "binanceus"), ("Kucoin", "kucoin")}
    lines = cohorts.pick_archive(elites, pd.DataFrame(), ok, DIV, {"binanceus": 10})
    assert [l.split()[2] for l in lines] == ["Both", "Kucoin"]
    # Negative control: with no replicating venue named, nothing replicates.
    assert cohorts.pick_archive(elites, pd.DataFrame(), ok, {**DIV, "replicate_on": []}, {"binanceus": 10}) == []


def test_a_diverse_pick_over_its_candle_budget_is_refused_not_trimmed():
    with pytest.raises(ValueError, match="2 bots on binanceus make 20.0 candle calls a minute, over its budget of 10"):
        cohorts.pick_archive([elite("A", 0.9, "1m"), elite("B", 0.5, "1m")], pd.DataFrame(), edged("A", "B"), DIV,
                             {"binanceus": 10})


def test_the_shipped_diverse_cohort_fits_its_budget_at_max_bots(tmp_path):
    cfg = {"prefix": "div", "cohort": "diverse", **cohorts.load()["diverse"], "max_rho": 0.5}
    elites = [elite(f"S{i}", 1.0 - i / 100, cfg["min_timeframe"]) for i in range(cfg["max_bots"] + 4)]
    ok = edged(*(e["strategy"] for e in elites), on=cfg["venues"] + cfg["replicate_on"][:1])
    lines = cohorts.pick_archive(elites, pd.DataFrame(), ok, cfg, {v: len(analyze.pair_list(v)) for v in cfg["venues"]})
    assert len(lines) == cfg["max_bots"]
    roster = tmp_path / "roster.tsv"
    roster.write_text("\n".join(lines))
    assert {r.cohort for r in league.roster_rows(roster)} == {"diverse"}


def test_archive_elites_keep_the_timeframe_floor_and_venue(conn):
    conn.execute("INSERT INTO strategies (name, file, file_hash, timeframe) VALUES ('Slow', 'f', 'h', '4h'), "
                 "('Fast', 'f', 'h', '5m')")  # one discovery: one seen_at
    for name in ("Slow", "Fast"):
        for ex in ("binanceus", "okx"):
            conn.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades, sharpe_lo, gross_expectancy) "
                         "VALUES (%s, %s, '365d', 365, 60, 0.4, %s)", (name, ex, 0.001 if name == "Slow" else -0.001))
    dcfg = {"window": "365d", "min_trades": 30, "hold_hours": [1, 24, 168], "freq_buckets": 3, "max_rho": 0.5,
            "skip_families": []}
    got = cohorts.archive_elites(conn, {"venues": ["binanceus"], "min_timeframe": "1h"}, dcfg, {})
    assert [(e["strategy"], e["exchange"]) for e in got] == [("Slow", "binanceus")]
    assert cohorts.edges(conn, "365d") == {("Slow", "binanceus"), ("Slow", "okx")}
    assert cohorts.edges(conn, "90d") == set()
    conn.execute("INSERT INTO strategy_daily VALUES ('Slow', 'binanceus', '2026-01-01', 0.01, 0, 1)")
    assert cohorts.daily_rows(conn, [("Slow", "binanceus")]) == [("Slow", "binanceus", dt.date(2026, 1, 1), 0.01)]


LOOP_ROSTER = "# name exchange strategy\n\nkraken-sample        kraken     SampleStrategy\nkraken-b  kraken  B\n"
LOOP_STS = "spec:\n  serviceName: x\n  replicas: 2  # rows\n  template: {}\n"


def test_the_roster_gains_one_row_and_replicas_follow():
    line = suggest.roster_line("Good", "kraken")
    roster, sts = cohort_roster.roster_change(LOOP_ROSTER, LOOP_STS, line)
    assert roster.endswith(line + "\n") and cohort_roster.rows_of(roster)[:2] == cohort_roster.rows_of(LOOP_ROSTER)
    assert "  replicas: 3  # rows\n" in sts and sts.replace("3  #", "2  #") == LOOP_STS
    assert cohort_roster.roster_change(roster, sts, line) is None


def test_the_roster_refuses_a_statefulset_already_out_of_step():
    with pytest.raises(ValueError, match="replicas 5 for 2 roster rows"):
        cohort_roster.roster_change(LOOP_ROSTER, LOOP_STS.replace("2  #", "5  #"), suggest.roster_line("Good", "kraken"))
