import importlib.util
import pathlib

import pytest

from lab import analyze, cohorts, league, variants

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


DIV = {"prefix": "div", "cohort": "diverse", "venues": ["binanceus"], "min_timeframe": "1h", "max_bots": 16,
       "max_rho": 0.5, "budget": {"binanceus": 10}}
DAYS = [f"d{i:03}" for i in range(120)]


def cell(strategy, lo, family="trend", timeframe="1h", exchange="binanceus", hold="1h to 24h"):
    return {"strategy": strategy, "exchange": exchange, "sharpe_lo": lo, "timeframe": timeframe, "family": family,
            "hold": hold, "climate": "none", "freq": "mid"}


def series(seed):
    """A deterministic daily return series; seeds far apart are near uncorrelated, a scaled copy is rho 1."""
    import random
    rnd = random.Random(seed)
    return {d: rnd.gauss(0, 0.01) for d in DAYS}


def test_an_archive_cohort_takes_each_cells_elite_greedily_while_decorrelated():
    a = series(1)
    described = [cell("A", 0.9), cell("A2", 0.5),  # same cell: A is the elite, A2 never competes
                 cell("B", 0.8, family="range"), cell("C", 0.7, family="breakout"), cell("D", 0.6, family="clock")]
    daily = {("A", "binanceus"): a, ("B", "binanceus"): {d: 2 * r for d, r in a.items()},  # B copies A
             ("C", "binanceus"): series(3), ("D", "binanceus"): {d: r for d, r in list(series(4).items())[:30]}}
    lines = cohorts.pick_archive(described, daily, DIV, {"binanceus": 10})
    # B is rho 1 with A and is dropped; D shares too few days to be scored, so it passes as the max_rho gate does
    assert lines == ["div-binanceus-a binanceus A diverse base", "div-binanceus-c binanceus C diverse base",
                     "div-binanceus-d binanceus D diverse base"]
    assert cohorts.rho(a, daily[("B", "binanceus")]) == pytest.approx(1.0)
    assert cohorts.rho(a, daily[("D", "binanceus")]) is None


def test_an_archive_cohort_runs_only_slow_enough_strategies_on_its_venues_up_to_its_cap():
    described = [cell("Fast", 0.9, timeframe="5m"), cell("Slow", 0.1, timeframe="4h"),
                 cell("Okx", 0.8, family="range", exchange="okx")]
    # the fast elite gives way to the cell's best slow strategy; a research venue never gets a bot
    assert cohorts.pick_archive(described, {}, DIV, {"binanceus": 10}) == ["div-binanceus-slow binanceus Slow diverse base"]
    many = [cell(f"S{i}", i / 100, family=f"f{i}") for i in range(30)]
    lines = cohorts.pick_archive(many, {}, DIV, {"binanceus": 10})
    assert len(lines) == 16 and lines[0].split()[2] == "S29"


def test_an_archive_cohort_over_its_venue_budget_is_refused_not_trimmed():
    many = [cell(f"S{i}", i / 100, family=f"f{i}") for i in range(16)]
    with pytest.raises(ValueError, match="16 bots on binanceus make 2.7 candle calls a minute, over its budget of 2"):
        cohorts.pick_archive(many, {}, {**DIV, "budget": {"binanceus": 2}}, {"binanceus": 10})


def test_the_shipped_diverse_cohort_fits_its_budget_at_its_cap_and_league_accepts_its_rows(tmp_path):
    cfg = {"prefix": cohorts.prefixes(cohorts.load())["diverse"], "cohort": "diverse", **cohorts.load()["diverse"],
           "max_rho": 0.5}
    assert cfg["kind"] == "archive"
    many = [cell(f"S{i}", i / 100, family=f"f{i}", timeframe=cfg["min_timeframe"]) for i in range(40)]
    lines = cohorts.pick_archive(many, {}, cfg, {v: len(analyze.pair_list(v)) for v in cfg["venues"]})
    assert len(lines) == cfg["max_bots"]
    roster = tmp_path / "roster.tsv"
    roster.write_text("\n".join(lines))
    assert {r.cohort for r in league.roster_rows(roster)} == {"diverse"}


def test_daily_rows_are_the_newest_corr_days_of_the_asked_series(conn):
    import datetime
    start = datetime.date(2025, 1, 1)
    for s in ("A", "B"):
        for i in range(analyze.CORR_DAYS + 5):
            conn.execute("INSERT INTO strategy_daily (strategy, exchange, day, ret, pnl, trades) "
                         "VALUES (%s, 'binanceus', %s, %s, 0, 0)", (s, start + datetime.timedelta(days=i), i))
    got = cohorts.daily_rows(conn, [("A", "binanceus")])
    assert list(got) == [("A", "binanceus")] and len(got[("A", "binanceus")]) == analyze.CORR_DAYS
    assert min(got[("A", "binanceus")].values()) == 5
