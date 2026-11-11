#!/usr/bin/env python3
"""Pick the roster rows of a dry-run cohort from the analysis tables (lab/cohorts.toml).

`python -m lab.cohorts pick <cohort>` prints a JSON list of roster lines. A strategy cohort (aggressive) is the top
strategies by the low end of the Sharpe interval in the cohort's window, each with the setup overlay that scored best
for it, one row per venue; lookahead-biased or unloadable strategies are left out, since their backtests are void. A
mixture cohort (`kind = "mixture"`) is the sleeves of the best mixture lab/mixtures.py scored that bots can run as
specified, one row per sleeve on its own exchange. An all cohort (`kind = "all"`) is every lookahead-clean, loadable
strategy outside its skipped families, one row per venue, refused when its candle calls would pass the venue's
budget. An archive cohort (`kind = "archive"`) is one elite per filled cell of the diversity archive
(lab/diversity.py) on a slow enough timeframe, kept greedily while its daily returns stay decorrelated from those
already kept. The promote gates are not applied: a cohort forward-tests near-misses. Nothing here edits the roster:
ci/cohort_roster.py turns the rows into a home-config PR that a person merges.
"""

import json
import math
import os
import statistics
import sys
import tomllib
from pathlib import Path

from freqtrade.exchange import timeframe_to_minutes

from psycopg.rows import dict_row

from lab import analyze, diversity, variants
from lab.db import connect

PATH = Path(os.environ.get("COHORTS_CONFIG", Path(__file__).with_name("cohorts.toml")))
FAMILIES = Path(__file__).with_name("families.toml")
BASE = "base"  # the roster's word for no overlay


def load(path: Path | None = None) -> dict:
    return tomllib.loads((path or PATH).read_text())


def prefixes(cfgs: dict) -> dict[str, str]:
    """Pure: each cohort's roster-name prefix, its first three letters. Raises ValueError when two cohorts share one:
    their rows would be told apart only by the cohort column, and a name already in the roster blocks the other."""
    seen: dict[str, str] = {}
    for name in cfgs:
        other = seen.setdefault(name[:3], name)
        if other != name:
            raise ValueError(f"cohorts {other!r} and {name!r} share the roster prefix {name[:3]!r}: rename one")
    return {name: name[:3] for name in cfgs}


def score_rows(conn, window: str, venues: list[str], min_trades: int) -> list[tuple]:
    """(strategy, exchange, overlay, sharpe_lo) for every lookahead-clean, loadable strategy's window, overlay '' for
    the strategy as it stands."""
    return list(conn.execute(
        "SELECT w.strategy, w.exchange, w.overlay, w.sharpe_lo FROM ("
        " SELECT strategy, exchange, '' AS overlay, win, trades, sharpe_lo FROM strategy_windows"
        " UNION ALL SELECT strategy, exchange, overlay, win, trades, sharpe_lo FROM overlay_windows) w "
        "JOIN strategies s ON s.name = w.strategy AND s.load_error IS NULL "
        "WHERE w.win = %s AND w.exchange = ANY(%s) AND w.trades >= %s AND w.sharpe_lo IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM bias_checks b WHERE b.strategy = w.strategy AND b.kind = 'lookahead' AND b.has_bias)",
        (window, venues, min_trades)))


def pick(rows: list[tuple], cfg: dict, known: set[str]) -> list[str]:
    """Pure: roster lines for one cohort's config. A (strategy, overlay) scores the mean low end across rank_venues
    and counts only with a row on each of them and an overlay id in `known`; a strategy keeps its best-scoring
    overlay (the base wins a tie); the top strategies by that score get one row per run venue."""
    ranked = cfg["rank_venues"]
    by: dict[tuple[str, str], dict[str, float]] = {}
    for strategy, exchange, overlay, lo in rows:
        if overlay == "" or overlay in known:
            by.setdefault((strategy, overlay), {})[exchange] = lo
    best: dict[str, tuple[float, str]] = {}
    for (strategy, overlay), got in by.items():
        if set(got) >= set(ranked):
            score = statistics.fmean(got[v] for v in ranked)
            key = (score, overlay == "")
            if strategy not in best or key > (best[strategy][0], best[strategy][1] == ""):
                best[strategy] = (score, overlay)
    order = sorted(best, key=lambda s: (-best[s][0], s))[:cfg["top"]]
    return [f"{cfg['prefix']}-{v}-{s.lower()} {v} {s} {cfg['cohort']} {best[s][1] or BASE}"
            for s in order for v in cfg["venues"]]


def mixture_rows(conn, weighting: str, gated: bool, min_days: int) -> list[tuple]:
    """(variant, sleeves, lo, sharpe) of every mixture of that weighting and gate with an interval over min_days."""
    return list(conn.execute(
        "SELECT variant, spec->'sleeves', lo, sharpe FROM mixtures "
        "WHERE weighting = %s AND gated = %s AND days >= %s AND lo IS NOT NULL",
        (weighting, gated, min_days)))


def pick_mixture(rows: list[tuple], cfg: dict) -> list[str]:
    """Pure: roster lines for the best mixture by the low end of its interval (then Sharpe, then variant), one per
    sleeve on the sleeve's own exchange, each under the base overlay: sleeves are walk-forward returns, not overlays."""
    if not rows:
        return []
    _, sleeves, _, _ = min(rows, key=lambda r: (-r[2], -(r[3] if r[3] is not None else -math.inf), r[0]))
    return [f"{cfg['prefix']}-{e}-{s.lower()} {e} {s} {cfg['cohort']} {BASE}" for s, e in sleeves]


def strategy_rows(conn) -> list[tuple]:
    """(strategy, timeframe) of every strategy the newest discovery found that loads and has no lookahead bias."""
    return list(conn.execute(
        "SELECT s.name, s.timeframe FROM strategies s WHERE s.load_error IS NULL "
        "AND s.seen_at = (SELECT max(seen_at) FROM strategies) "
        "AND NOT EXISTS (SELECT 1 FROM bias_checks b WHERE b.strategy = s.name AND b.kind = 'lookahead' AND b.has_bias)"))


def calls_per_minute(timeframe: str, pairs: int) -> float:
    """OHLCV calls a bot makes a minute: one per pair per new candle."""
    return pairs / timeframe_to_minutes(timeframe)


def pick_all(rows: list[tuple], cfg: dict, family: dict[str, str], pairs: dict[str, int]) -> list[str]:
    """Pure: one roster line per strategy per venue, base overlay, for every strategy not in a skipped family (an
    untagged one runs). Raises ValueError when a venue's candle calls would pass its budget: shrink the cohort or
    raise the budget in the toml, never trim it silently."""
    keep = sorted((s, tf) for s, tf in rows if family.get(s) not in cfg["skip_families"])
    check_budget(cfg, {v: [tf for _, tf in keep] for v in cfg["venues"]}, pairs)
    return [f"{cfg['prefix']}-{v}-{s.lower()} {v} {s} {cfg['cohort']} {BASE}" for s, _ in keep for v in cfg["venues"]]


def check_budget(cfg: dict, timeframes: dict[str, list[str]], pairs: dict[str, int]) -> None:
    """Raises ValueError when a venue's candle calls (its bots' timeframes) would pass its budget in the toml."""
    for v, tfs in timeframes.items():
        calls = sum(calls_per_minute(tf, pairs[v]) for tf in tfs)
        if calls > cfg["budget"][v]:
            raise ValueError(f"{cfg['cohort']}: {len(tfs)} bots on {v} make {round(calls, 1):g} candle calls a minute, "
                             f"over its budget of {cfg['budget'][v]}")


def daily_rows(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """(strategy, exchange) -> {day: return} over the newest CORR_DAYS of each series, the span the max_rho gate's
    correlations use (lab/analyze.py)."""
    out: dict[tuple[str, str], dict] = {}
    for s, e, day, ret in conn.execute(
            "SELECT strategy, exchange, day, ret FROM ("
            " SELECT strategy, exchange, day, ret, row_number() OVER (PARTITION BY strategy, exchange ORDER BY day DESC) n"
            " FROM strategy_daily WHERE (strategy, exchange) IN (SELECT * FROM unnest(%s::text[], %s::text[]))) d "
            "WHERE n <= %s", ([k[0] for k in keys], [k[1] for k in keys], analyze.CORR_DAYS)):
        out.setdefault((s, e), {})[day] = ret
    return out


def rho(a: dict, b: dict) -> float | None:
    """Pearson correlation of two daily return series over their shared days; None under CORR_MIN_DAYS shared days
    or when either is flat, as the max_rho gate leaves such a pair unscored."""
    days = sorted(a.keys() & b.keys())
    if len(days) < analyze.CORR_MIN_DAYS:
        return None
    x, y = [a[d] for d in days], [b[d] for d in days]
    if len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return statistics.correlation(x, y)


def archive_elites(described: list[dict], cfg: dict) -> list[dict]:
    """Pure: the elite of each filled cell among the strategies a bot of this cohort can run (a venue in `venues`,
    a timeframe of `min_timeframe` or slower), best low end first. Filtering before electing keeps a cell whose
    fastest strategy is too fast for the candle budget."""
    floor = timeframe_to_minutes(cfg["min_timeframe"])
    runnable = [r for r in described if r["exchange"] in cfg["venues"] and r["timeframe"]
                and timeframe_to_minutes(r["timeframe"]) >= floor]
    return sorted(diversity.elites(runnable).values(), key=lambda r: (-r["sharpe_lo"], r["strategy"], r["exchange"]))


def pick_archive(described: list[dict], daily: dict[tuple[str, str], dict], cfg: dict, pairs: dict[str, int]) -> list[str]:
    """Pure: roster lines, base overlay, for the archive's elites taken greedily by low end: each next one joins only
    when its largest |rho| to those taken is `max_rho` or under (a pair with too few shared days is unscored and
    passes, as in the max_rho gate), one row per strategy, until `max_bots`. Raises ValueError over the budget."""
    taken: list[dict] = []
    for r in archive_elites(described, cfg):
        if len(taken) == cfg["max_bots"]:
            break
        if any(t["strategy"] == r["strategy"] for t in taken):
            continue
        mine = daily.get((r["strategy"], r["exchange"]), {})
        rhos = [rho(mine, daily.get((t["strategy"], t["exchange"]), {})) for t in taken]
        if all(x is None or abs(x) <= cfg["max_rho"] for x in rhos):
            taken.append(r)
    by_venue: dict[str, list[str]] = {}
    for r in taken:
        by_venue.setdefault(r["exchange"], []).append(r["timeframe"])
    check_budget(cfg, by_venue, pairs)
    return [f"{cfg['prefix']}-{r['exchange']}-{r['strategy'].lower()} {r['exchange']} {r['strategy']} {cfg['cohort']} {BASE}"
            for r in taken]


def archive_described(conn) -> list[dict]:
    """The diversity archive's rows with their five descriptors, as /coverage builds them."""
    dcfg = diversity.config()
    cur = conn.cursor(row_factory=dict_row)
    rows = cur.execute(diversity.ROWS, (dcfg["window"], dcfg["min_trades"])).fetchall()
    climates = cur.execute(diversity.CLIMATE_ROWS).fetchall()
    return diversity.describe(rows, families(), diversity.best_climates(climates), dcfg)


def families() -> dict[str, str]:
    return {s: t["family"] for s, t in tomllib.loads(FAMILIES.read_text())["strategies"].items()}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "pick":
        print("usage: python -m lab.cohorts pick <cohort>", file=sys.stderr)
        return 2
    cfgs = load()
    if argv[1] not in cfgs:
        print(f"no cohort {argv[1]!r} in {PATH}", file=sys.stderr)
        return 2
    try:
        prefix = prefixes(cfgs)[argv[1]]
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    cfg = {"prefix": prefix, "cohort": argv[1], **cfgs[argv[1]]}
    with connect() as conn:
        if cfg.get("kind") == "all":
            try:
                lines = pick_all(strategy_rows(conn), cfg, families(), {v: len(analyze.pair_list(v)) for v in cfg["venues"]})
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1
        elif cfg.get("kind") == "archive":
            cfg["max_rho"] = diversity.config()["max_rho"]
            described = archive_described(conn)
            keys = [(r["strategy"], r["exchange"]) for r in archive_elites(described, cfg)]
            try:
                lines = pick_archive(described, daily_rows(conn, keys), cfg,
                                     {v: len(analyze.pair_list(v)) for v in cfg["venues"]})
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1
        elif cfg.get("kind") == "mixture":
            lines = pick_mixture(mixture_rows(conn, cfg["weighting"], cfg["gated"], cfg["min_days"]), cfg)
        else:
            known, _ = variants.load()
            lines = pick(score_rows(conn, cfg["window"], cfg["rank_venues"], cfg["min_trades"]), cfg, set(known))
    print(json.dumps(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
