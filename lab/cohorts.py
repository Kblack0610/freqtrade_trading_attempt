#!/usr/bin/env python3
"""Pick the roster rows of a dry-run cohort from the analysis tables (lab/cohorts.toml).

`python -m lab.cohorts pick <cohort>` prints a JSON list of roster lines. A strategy cohort (aggressive) is the top
strategies by the low end of the Sharpe interval in the cohort's window, each with the setup overlay that scored best
for it, one row per venue; lookahead-biased or unloadable strategies are left out, since their backtests are void. A
mixture cohort (`kind = "mixture"`) is the sleeves of the best mixture lab/mixtures.py scored that bots can run as
specified, one row per sleeve on its own exchange. The promote gates are not applied: a cohort forward-tests
near-misses. Nothing here edits the roster: ci/cohort_roster.py turns the rows into a home-config PR that a person
merges.
"""

import json
import math
import os
import statistics
import sys
import tomllib
from pathlib import Path

from lab import variants
from lab.db import connect

PATH = Path(os.environ.get("COHORTS_CONFIG", Path(__file__).with_name("cohorts.toml")))
BASE = "base"  # the roster's word for no overlay


def load(path: Path | None = None) -> dict:
    return tomllib.loads((path or PATH).read_text())


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


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "pick":
        print("usage: python -m lab.cohorts pick <cohort>", file=sys.stderr)
        return 2
    cfg = load().get(argv[1])
    if cfg is None:
        print(f"no cohort {argv[1]!r} in {PATH}", file=sys.stderr)
        return 2
    cfg = {"prefix": argv[1][:3], "cohort": argv[1], **cfg}
    with connect() as conn:
        if cfg.get("kind") == "mixture":
            lines = pick_mixture(mixture_rows(conn, cfg["weighting"], cfg["gated"], cfg["min_days"]), cfg)
        else:
            known, _ = variants.load()
            lines = pick(score_rows(conn, cfg["window"], cfg["rank_venues"], cfg["min_trades"]), cfg, set(known))
    print(json.dumps(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
