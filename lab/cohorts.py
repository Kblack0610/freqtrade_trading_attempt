#!/usr/bin/env python3
"""Pick the roster rows of a dry-run cohort from the analysis tables (lab/cohorts.toml).

`python -m lab.cohorts pick <cohort>` prints a JSON list of roster lines. A strategy cohort (aggressive) is the top
strategies by the low end of the Sharpe interval in the cohort's window, each with the setup overlay that scored best
for it, one row per venue; lookahead-biased or unloadable strategies are left out, since their backtests are void. A
mixture cohort (`kind = "mixture"`) is the sleeves of the best mixture lab/mixtures.py scored that bots can run as
specified, one row per sleeve on its own exchange. An all cohort (`kind = "all"`) is every lookahead-clean, loadable
strategy outside its skipped families, one row per venue, refused when its candle calls would pass the venue's
budget. An archive cohort (`kind = "archive"`) is one elite per filled cell of the diversity archive (lab/diversity.py)
on its venues and timeframe floor, best Sharpe low end first, keeping one only when its edge before fees also shows
on a `replicate_on` venue, its BTC-residual daily returns correlate at most `[diversity] max_rho` with every one
already picked, and it lifts the picks' effective count by `min_neff_lift`, up to `max_bots`. The promote gates are not applied: a cohort
forward-tests near-misses. Nothing here edits the roster: ci/cohort_roster.py turns the rows into a home-config PR that a person
merges.
"""

import json
import math
import os
import statistics
import sys
import tomllib
from pathlib import Path

from freqtrade.exchange import timeframe_to_minutes

import pandas as pd
from psycopg.rows import dict_row

from lab import analyze, diversity, metrics, variants
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
    for v in cfg["venues"]:
        calls = sum(calls_per_minute(tf, pairs[v]) for _, tf in keep)
        if calls > cfg["budget"][v]:
            raise ValueError(f"{cfg['cohort']}: {len(keep)} bots on {v} make {calls:.0f} candle calls a minute, "
                             f"over its budget of {cfg['budget'][v]}")
    return [f"{cfg['prefix']}-{v}-{s.lower()} {v} {s} {cfg['cohort']} {BASE}" for s, _ in keep for v in cfg["venues"]]


def archive_elites(conn, cfg: dict, dcfg: dict, family: dict[str, str]) -> list[dict]:
    """The elite of every archive cell on the cohort's venues among strategies at its timeframe floor or slower. The
    cells are described over every strategy first, so the frequency buckets match /coverage."""
    cur = conn.cursor(row_factory=dict_row)
    described = diversity.describe(cur.execute(diversity.ROWS, (dcfg["window"], dcfg["min_trades"])).fetchall(), family,
                                   diversity.best_climates(cur.execute(diversity.CLIMATE_ROWS).fetchall()), dcfg)
    floor = timeframe_to_minutes(cfg["min_timeframe"])
    return list(diversity.elites([r for r in described if r["exchange"] in cfg["venues"] and r["timeframe"]
                                  and timeframe_to_minutes(r["timeframe"]) >= floor]).values())


def daily_rows(conn, keys: list[tuple[str, str]]) -> list[tuple]:
    """(strategy, exchange, day, ret) of the given strategy x exchange series."""
    if not keys:
        return []
    return list(conn.execute("SELECT strategy, exchange, day, ret FROM strategy_daily "
                             "WHERE (strategy, exchange) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
                             ([k[0] for k in keys], [k[1] for k in keys])))


def btc_returns(conn) -> pd.Series:
    """BTC's daily return by day, from the regimes table's close."""
    got = conn.execute("SELECT day, btc_close FROM regimes ORDER BY day").fetchall()
    return pd.Series([c for _, c in got], index=[d for d, _ in got], dtype=float).pct_change().dropna()


def replicated(conn, window: str, venues: list[str]) -> set[str]:
    """Strategies with a positive mean edge before fees in `window` on any of `venues`: an edge that shows on more
    than one exchange's prices is less likely one venue's quirk."""
    return {s for (s,) in conn.execute("SELECT DISTINCT strategy FROM strategy_windows WHERE win = %s "
                                       "AND exchange = ANY(%s) AND gross_expectancy > 0", (window, venues))}


def residuals(rows: list[tuple], btc: pd.Series) -> pd.DataFrame:
    """Pure: each series' daily return over its last analyze.CORR_DAYS days with BTC taken out (less beta x BTC's
    return, beta by least squares over their shared days), one column per (strategy, exchange). The market is the
    one return source most strategies share, so what is left is what each strategy adds."""
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=["strategy", "exchange", "day", "ret"])
    wide = frame.pivot_table(index="day", columns=["strategy", "exchange"], values="ret").sort_index()
    out = {}
    for col in wide.columns:
        r = wide[col].dropna().iloc[-analyze.CORR_DAYS:]
        both = pd.concat([r, btc], axis=1, join="inner").dropna()
        var = both.iloc[:, 1].var()
        beta = both.iloc[:, 0].cov(both.iloc[:, 1]) / var if len(both) > 1 and var > 0 else 0.0
        out[col] = r - beta * btc.reindex(r.index).fillna(0.0)
    return pd.DataFrame(out)


def pick_archive(elites: list[dict], resid: pd.DataFrame, replicas: set[str], cfg: dict, pairs: dict[str, int]) -> list[str]:
    """Pure: the greedy diverse pick. Elites in order of Sharpe low end (then name), each joining when (1) its edge
    replicates (in `replicas`), (2) its BTC-residual |rho| to every one already picked is at most cfg["max_rho"],
    and (3) it lifts the picks' effective count (metrics.effective_trials on the residuals) by at least
    cfg["min_neff_lift"], until cfg["max_bots"]. A pair with too few shared days to measure counts as different.
    One row per pick on its own venue, base overlay. Raises ValueError when a venue's candle calls would pass its
    budget."""
    rho = resid.corr(min_periods=analyze.CORR_MIN_DAYS) if not resid.empty else pd.DataFrame()
    picked: list[dict] = []
    neff = 0.0
    for e in sorted(elites, key=lambda e: (-e["sharpe_lo"], e["strategy"], e["exchange"])):
        if len(picked) == cfg["max_bots"]:
            break
        key = (e["strategy"], e["exchange"])
        if e["strategy"] not in replicas:
            continue
        keys = [(p["strategy"], p["exchange"]) for p in picked]
        if key in rho.columns and any(k in rho.columns and abs(rho.loc[key, k]) > cfg["max_rho"] for k in keys):
            continue
        cols = [k for k in keys + [key] if k in resid.columns]
        after = metrics.effective_trials(resid[cols], analyze.CORR_MIN_DAYS) + (len(keys) + 1 - len(cols))
        if after - neff < cfg["min_neff_lift"]:
            continue
        picked.append(e)
        neff = after
    for v in cfg["venues"]:
        mine = [e for e in picked if e["exchange"] == v]
        calls = sum(calls_per_minute(e["timeframe"], pairs[v]) for e in mine)
        if calls > cfg["budget"][v]:
            raise ValueError(f"{cfg['cohort']}: {len(mine)} bots on {v} make {calls:.1f} candle calls a minute, "
                             f"over its budget of {cfg['budget'][v]}")
    return [f"{cfg['prefix']}-{e['exchange']}-{e['strategy'].lower()} {e['exchange']} {e['strategy']} {cfg['cohort']} {BASE}"
            for e in picked]


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
            dcfg = diversity.config()
            elites = archive_elites(conn, cfg, dcfg, families())
            resid = residuals(daily_rows(conn, [(e["strategy"], e["exchange"]) for e in elites]), btc_returns(conn))
            try:
                lines = pick_archive(elites, resid, replicated(conn, dcfg["window"], cfg["replicate_on"]),
                                     {**cfg, "max_rho": dcfg["max_rho"]},
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
