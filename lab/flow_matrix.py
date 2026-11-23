#!/usr/bin/env python3
"""Flow matrix: every strategy of the book through the backtest, bias and book avenues, on synthetic candles.

Each cell calls the real stage code (analyze.Cli().backtest, analyze.bias_one, book.rows) in its own process against a
temporary REPO holding a copy of user_data/strategies and seeded candles from lab/calibration.py, so nothing here
reads a venue, the database or the production results, and a strategy that crashes or leaks takes only its cell.
A cell is pass, fail or skip (nothing to judge: no trades, too few signals, too few months). One synthetic venue,
named binanceus for its fees, stands for the nightly's venues: the code under test does not branch on the venue.

Run `python -m lab.flow_matrix [--sample] [NAME ...]`; it prints the matrix and exits 1 on any fail.
lab/tests/test_flow_matrix.py runs the sample (one strategy per family) in CI (test-calibration job).
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from lab import book, calibration, suggest

REPO = Path(__file__).resolve().parents[1]
EXCHANGE = calibration.EXCHANGE
AVENUES = ("backtest", "bias", "book")
PAIRS = ["BTC/USD", "ETH/USD"]
SEED = 11
CANDLE_DAYS = 420  # warm-up for the longest startup_candle_count on 1d, then the backtest's own window
BACKTEST_DAYS = 240  # eight months: more than [confidence] min_folds, so the book avenue has months to project
LOOKAHEAD_DAYS = "20"
TIMEFRAMES = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1D"}
OUT_OF_BOOK = {"ml", "short"}  # ml needs the trader-ml image; short is research outside user_data
CELL_TIMEOUT = 900
# a check with nothing to judge: too few signals, or the strategy sets no startup_candle_count (the nightly records it)
NOTHING_TO_JUDGE = re.compile(r"signals in \d+ days|invalid startup candle count")


def families() -> dict[str, str]:
    """Strategy class -> family, from lab/families.toml (the one list that classifies the book)."""
    return {k: v["family"] for k, v in tomllib.loads((REPO / "lab" / "families.toml").read_text())["strategies"].items()}


def strategies(names: list[str] | None = None) -> list[dict]:
    """The strategies the nightly runs: what analyze discovers, minus a class that does not load, with its family."""
    from lab import analyze

    fam = families()
    found = [{"name": s["name"], "file": s["file"], "family": fam.get(s["name"]), "load_error": s["load_error"]}
             for s in analyze.Cli().discover()]
    found = [s for s in found if not names or s["name"] in names]
    return sorted(found, key=lambda s: s["name"])


def sample(found: list[dict]) -> list[dict]:
    """The gate's strategies: the first of each family that runs on the trader image."""
    seen, out = set(), []
    for s in found:
        if s["family"] in OUT_OF_BOOK or s["load_error"] or s["family"] in seen:
            continue
        seen.add(s["family"])
        out.append(s)
    return out


def write_world(root: Path, today: dt.date, source: Path = REPO) -> dict:
    """The temporary REPO: the real base config, the synthetic venue and a copy of every strategy, and its candles.
    The candles run through the current hour, so the bias checks' last-hour windows (they read back from now) find data."""
    ud = root / "repo" / "user_data"
    (ud / "exchanges").mkdir(parents=True, exist_ok=True)
    shutil.copy(source / "user_data" / "config.base.json", ud / "config.base.json")
    venue = json.loads((source / "user_data" / "exchanges" / f"{EXCHANGE}.json").read_text())
    venue["exchange"]["pair_whitelist"] = PAIRS
    (ud / "exchanges" / f"{EXCHANGE}.json").write_text(json.dumps(venue, indent=2))
    shutil.copytree(source / "user_data" / "strategies", ud / "strategies", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    from freqtrade.misc import pair_to_filename

    data = root / "results" / "data" / EXCHANGE
    data.mkdir(parents=True, exist_ok=True)
    c = {**calibration.settings(), "pairs": PAIRS}
    closes = {}
    now = pd.Timestamp.now(tz="UTC")
    for pair in PAIRS:
        bars = calibration.candles(SEED, pair, today - dt.timedelta(days=CANDLE_DAYS), today + dt.timedelta(days=1), c)
        minutes = bars["1m"].set_index("date")
        minutes = minutes[minutes.index < now.floor("h") + pd.Timedelta(hours=1)]  # through the current hour's candle
        for tf, rule in TIMEFRAMES.items():
            df = minutes.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                             "volume": "sum"}).reset_index()
            df.to_feather(data / f"{pair_to_filename(pair)}-{tf}.feather")
        closes[pair] = minutes["close"].resample("1D").last()
    return {"repo": root / "repo", "results": root / "results", "closes": closes}


def cell_env(world: dict) -> dict:
    return {**os.environ, "REPO": str(world["repo"]), "LAB_RESULTS": str(world["results"]), "BIAS_EXCHANGE": EXCHANGE,
            "ANALYZE_EXCHANGES": EXCHANGE, "LOOKAHEAD_DAYS": LOOKAHEAD_DAYS, "PYTHONPATH": str(REPO),
            "DATABASE_URL": "postgresql://unused.invalid/none"}


def monthly_returns(trades: pd.DataFrame, balance: float) -> list[tuple[dt.date, dt.date, float]]:
    """Each calendar month's closed profit over the starting balance, the folds book.rows reads."""
    if trades.empty:
        return []
    month = pd.to_datetime(trades["close_date"], utc=True).dt.tz_localize(None).dt.to_period("M")
    by = trades.groupby(month)["profit_abs"].sum()
    return [(p.start_time.date(), (p.start_time + pd.DateOffset(months=1)).date(), float(v) / balance)
            for p, v in by.items()]


def cell(avenue: str, name: str) -> dict:
    """One cell in this process, with REPO and LAB_RESULTS already pointing at the world (see run_cell)."""
    from lab import analyze

    today = dt.datetime.now(dt.timezone.utc).date()
    if avenue == "backtest":
        r = analyze.Cli().backtest(name, EXCHANGE, today - dt.timedelta(days=BACKTEST_DAYS), today)
        if not r["ok"]:
            return {"status": "fail", "detail": r["error"]}
        t = r["trades"]
        bad = int((pd.to_datetime(t["close_date"]) < pd.to_datetime(t["open_date"])).sum()) if len(t) else 0
        prices = t[["open_rate", "close_rate"]].to_numpy(dtype=float) if len(t) else np.ones((0, 2))
        if bad or not np.isfinite(prices).all() or not np.isfinite(r["final_balance"]):
            return {"status": "fail", "detail": f"{len(t)} trades, {bad} close before open, or a non-finite price"}
        return {"status": "pass", "detail": f"{len(t)} trades",
                "trades": len(t), "start_balance": r["start_balance"], "months": [[a.isoformat(), b.isoformat(), v] for a, b, v in
                                                                                monthly_returns(t, r["start_balance"])]}
    if avenue == "bias":
        out = {}
        for kind in ("lookahead", "recursive"):
            out[kind] = analyze.Cli().bias(kind, name)
        errors = [f"{k}: {v['error']}" for k, v in out.items() if v.get("error") and v.get("has_bias") is None
                  and not NOTHING_TO_JUDGE.search(v["error"])]
        if errors:
            return {"status": "fail", "detail": " | ".join(errors)[:800], "bias": out}
        ran = [k for k, v in out.items() if v.get("has_bias") is not None]
        if not ran:
            return {"status": "skip", "detail": out["lookahead"].get("error"), "bias": out}
        flagged = [k for k in ran if out[k]["has_bias"]]
        return {"status": "pass", "detail": "bias: " + (", ".join(flagged) or "none"), "bias": out,
                "has_bias": {k: out[k]["has_bias"] for k in ran}}
    raise ValueError(avenue)


def run_cell(world: dict, avenue: str, name: str) -> dict:
    p = subprocess.run([sys.executable, "-m", "lab.flow_matrix", "cell", avenue, name], env=cell_env(world),
                       capture_output=True, text=True, timeout=CELL_TIMEOUT, cwd=REPO)
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("CELL "):
            return json.loads(line.removeprefix("CELL "))
    tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
    return {"status": "fail", "detail": f"exit {p.returncode}: {' | '.join(tail)}"[:800]}


def book_cell(backtest: dict, world: dict, cfg: dict) -> dict:
    """The strategy's backtest months through book.rows, as the book reads a walk-forward's."""
    if backtest["status"] != "pass":
        return {"status": "skip", "detail": "no backtest"}
    months = [(dt.date.fromisoformat(a), dt.date.fromisoformat(b), v) for a, b, v in backtest["months"]]
    full = months[1:-1]  # the first and last months are partial
    if len(full) < cfg["confidence"]["min_folds"]:
        return {"status": "skip", "detail": f"{len(full)} full months, below min_folds {cfg['confidence']['min_folds']}"}
    rows = book.rows({("s", EXCHANGE): full}, world["closes"]["BTC/USD"].rename(index=lambda d: d.tz_localize(None)), cfg)
    keys = ("p5", "p50", "p95", "prob_loss", "exp_max_drawdown")
    ok = len(rows) == 2 * len(cfg["book"]["horizon_months"]) and all(np.isfinite([r[k] for k in keys]).all() for r in rows)
    ordered = all(r["p5"] <= r["p50"] <= r["p95"] for r in rows)
    return {"status": "pass" if ok and ordered else "fail", "detail": f"{len(rows)} rows over {len(full)} months"}


def run(found: list[dict], world: dict, workers: int = 3) -> list[dict]:
    cfg = suggest.load_config()
    cells = [{"strategy": s["name"], "family": s["family"], "avenue": a, "status": "fail", "detail": s["load_error"]}
             for s in found if s["load_error"] for a in AVENUES]
    todo = [s for s in found if not s["load_error"]]
    with ThreadPoolExecutor(workers) as pool:
        bt = dict(zip([s["name"] for s in todo], pool.map(lambda s: run_cell(world, "backtest", s["name"]), todo)))
        bias = dict(zip([s["name"] for s in todo], pool.map(lambda s: run_cell(world, "bias", s["name"]), todo)))
    for s in todo:
        n = s["name"]
        for avenue, res in (("backtest", bt[n]), ("bias", bias[n]), ("book", book_cell(bt[n], world, cfg))):
            cells.append({"strategy": n, "family": s["family"], "avenue": avenue, **res})
    return cells


def summary(cells: list[dict]) -> dict:
    out = {a: {"pass": 0, "fail": 0, "skip": 0} for a in AVENUES}
    for c in cells:
        out[c["avenue"]][c["status"]] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["cell"]:
        print("CELL " + json.dumps(cell(args[1], args[2]), default=str), flush=True)
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", action="store_true", help="one strategy per family, as the CI gate runs")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the passing cells too")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args(args)
    found = strategies(a.names)
    if a.sample:
        found = sample(found)
    with tempfile.TemporaryDirectory(prefix="flow-matrix-") as d:
        cells = run(found, write_world(Path(d), dt.datetime.now(dt.timezone.utc).date()))
    for c in sorted(cells, key=lambda c: (c["status"] != "fail", c["strategy"], c["avenue"])):
        if a.verbose or c["status"] != "pass":
            print(f"{c['status']:4} {c['strategy']:32} {c['avenue']:8} {c['detail']}")
    print(json.dumps(summary(cells)))
    return int(any(c["status"] == "fail" for c in cells))


if __name__ == "__main__":
    sys.exit(main())
