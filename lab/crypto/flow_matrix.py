#!/usr/bin/env python3
"""Flow matrix: every strategy in the book through every avenue of the trader flow, on each exchange it is on.

The book is every strategy class the nightly analysis discovers under user_data/strategies, plus every class
lab/crypto/families.toml names outside lab/futures/research (the futures research, which the nightly flow never runs). The avenues, and the exchanges each
one runs a strategy on:
- backtest: its backtest chunks, on every analysis venue (ANALYZE_EXCHANGES). A class with no families.toml line
  fails here, as it shows untagged on /coverage.
- bias: the lookahead and recursive checks (lookahead only for a FreqAI strategy), on BIAS_EXCHANGE.
- walkforward: one fold (hyperopt, in-sample and out-of-sample backtests), on every walk-forward venue (WF_EXCHANGES).
- reproduce: one cached chunk and the fold rerun through lab/crypto/reproduce.py's checks, on the walk-forward venues.
- confidence: its readiness row, and whether the book took it, on the walk-forward venues.
- paper: the league's guard and a strategy load under the bot's own config files, on the venues of the
  `strategies` cohort (lab/crypto/cohorts.toml), which runs every strategy in dry run.
Each cell is ok, skip or fail, with its reason. A skip is a step the real flow leaves out on purpose (a family the
cohort skips, a bias check with nothing to judge, a FreqAI strategy on the plain image).

It runs the real stage code, as lab/crypto/calibration.py does: lab/crypto/analyze.py and lab/crypto/walkforward.py as subprocesses
against the scratch schema SCHEMA, with a temporary REPO holding the real strategies and a synthetic venue (seeded
random-walk candles under each venue's first PAIRS pairs). The analysis and walk-forward take January 1 as today
and the coverage starts a year before, so each strategy backtests one chunk (last year) and folds last December; a
backtest process's startup, not its candles, is most of a chunk's time. Only the run's own `runs` row (kind 'flow_matrix', the cells in
raw) reaches the production tables.

`python -m lab.crypto.flow_matrix [--sample] [NAME ...]` runs the whole book, the gate's sample, or the names. lab/tests/test_flow_matrix.py runs it with FLOW_MATRIX=gate on one
strategy per family (the flow_gate marker; CI's test-flow job, in shards) or FLOW_MATRIX=full on all of it.
`python -m lab.crypto.flow_matrix probe <spec.json>` is the reproduce and paper step, run in the scratch environment.
"""

import argparse
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import re
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

from lab.crypto import calibration, cohorts
from lab.core.db import connect

REPO = Path(__file__).resolve().parents[2]
AVENUES = ("backtest", "bias", "walkforward", "reproduce", "confidence", "paper")
SCHEMA = os.environ.get("FLOW_SCHEMA", "flow_matrix_scratch")
WORK = Path(os.environ.get("FLOW_WORK") or tempfile.gettempdir()) / "flow_matrix"
WORKERS = int(os.environ.get("FLOW_WORKERS", str(os.cpu_count() or 2)))
# The fold is a smoke run of the real code, not a tuning: a month of training and a few epochs.
TRAIN_DAYS = int(os.environ.get("FLOW_TRAIN_DAYS", "30"))
EPOCHS = int(os.environ.get("FLOW_EPOCHS", "6"))
PAIRS = 3  # the first pairs of each venue's whitelist
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
FREQ = {"1m": "min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "h", "4h": "4h", "1d": "D"}
MINUTE_VOL = 0.0015  # about 5.7% a day, crypto-like
START = {"BTC": 30_000.0, "ETH": 2_000.0, "SOL": 100.0}
RESEARCH_CLASS = re.compile(r"^class (\w+)\(", re.M)
CLEAN = ": clean"  # a bias cell's reason marks each check that reached a clean verdict
# a check with nothing to judge: too few signals (lab/crypto/analyze.py bias_one), or no startup_candle_count (the nightly records it)
NOTHING_TO_JUDGE = re.compile(r"signals in \d+ days|invalid startup candle count")


def synthetic_candles(start: dt.datetime, end: dt.datetime, freq: str, seed: int = 11, vol: float = 0.012,
                      price: float = 30_000.0) -> pd.DataFrame:
    """Random-walk candles from start to end at freq, with wicks and a volume that varies, so volume signals fire."""
    date = pd.date_range(start, end, freq=freq, tz="UTC")
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, vol, len(date))))
    opn = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, vol / 3, len(date)))
    return pd.DataFrame({"date": date, "open": opn, "high": np.maximum(opn, close) * (1 + wick),
                         "low": np.minimum(opn, close) * (1 - wick), "close": close,
                         "volume": 10.0 * np.exp(rng.normal(0, 0.8, len(date)))})


def resample(minutes: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "1m":
        return minutes
    out = minutes.set_index("date").resample(FREQ[tf]).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna().reset_index()


def research() -> set[str]:
    """The classes under lab/futures/research: futures research that families.toml tags but the nightly flow never runs."""
    return {m for f in (REPO / "lab" / "futures" / "research").glob("*.py") for m in RESEARCH_CLASS.findall(f.read_text())}


def book(found: list[dict], fams: dict[str, str]) -> list[str]:
    """Every discovered class, and every families.toml class outside lab/futures/research, in families.toml order then by name."""
    out = research()
    names = {s["name"] for s in found} | {n for n in fams if n not in out}
    order = {n: i for i, n in enumerate(fams)}
    return sorted(names, key=lambda n: (order.get(n, len(order)), n))


def sample(fams: dict[str, str]) -> list[str]:
    """The gate's fixed sample: the first class of each family in the book, in families.toml order."""
    seen: set[str] = set()
    skip, out = research(), []
    for name, fam in fams.items():
        if fam not in seen and name not in skip:
            seen.add(fam)
            out.append(name)
    return out


def venues() -> dict[str, list[str]]:
    """The exchanges each avenue runs a strategy on, as the production jobs read them."""
    from lab.crypto import analyze, walkforward

    return {"backtest": list(analyze.EXCHANGES), "bias": [analyze.BIAS_EXCHANGE],
            "walkforward": list(walkforward.EXCHANGES), "reproduce": list(walkforward.EXCHANGES),
            "confidence": list(walkforward.EXCHANGES), "paper": list(cohorts.load()["strategies"]["venues"])}


def span() -> tuple[dt.datetime, dt.datetime]:
    """From before the longest lookahead window (a 1d strategy's) and its startup, to the last closed hour."""
    from lab.crypto import analyze

    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    back = max(analyze.lookahead_days(tf) for tf in TIMEFRAMES) + 30 + 400
    return (end - dt.timedelta(days=back)).replace(day=1, hour=0), end


def write_repo(root: Path, exchanges: list[str], extra: tuple[Path, ...] = ()) -> Path:
    """A REPO with the real base config, each venue's config cut to its first PAIRS pairs, and the real strategies
    plus any extra strategy files."""
    from freqtrade.configuration.directory_operations import create_userdata_dir

    ud = root / "user_data"
    (ud / "exchanges").mkdir(parents=True, exist_ok=True)
    (ud / "config.base.json").write_text((REPO / "user_data" / "config.base.json").read_text())
    for ex in exchanges:
        venue = json.loads((REPO / "user_data" / "exchanges" / f"{ex}.json").read_text())
        venue["exchange"]["pair_whitelist"] = venue["exchange"]["pair_whitelist"][:PAIRS]
        (ud / "exchanges" / f"{ex}.json").write_text(json.dumps(venue, indent=2))
    create_userdata_dir(str(ud))  # Freqtrade's own layout, so parallel checks do not race to create a folder
    # Copied, not linked: Freqtrade's recursive strategy search does not follow a linked directory.
    shutil.copytree(REPO / "user_data" / "strategies", ud / "strategies", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".*"))
    for f in extra:
        (ud / "strategies" / f.name).write_text(f.read_text())
    return root


def write_candles(root: Path, results: Path, exchanges: list[str], start: dt.datetime, end: dt.datetime) -> list[tuple]:
    """Each venue's candle files at every timeframe from one minute series per coin, so the timeframes agree; a
    coin's pair against BTC (an informative pair) is its ratio. Returns data_coverage rows."""
    from freqtrade.misc import pair_to_filename

    minutes = {coin: synthetic_candles(start, end, "min", seed=11 + i, vol=MINUTE_VOL, price=p)
               for i, (coin, p) in enumerate(START.items())}
    rows = []
    for ex in exchanges:
        pairs = json.loads((root / "user_data" / "exchanges" / f"{ex}.json").read_text())["exchange"]["pair_whitelist"]
        data = results / "data" / ex
        data.mkdir(parents=True, exist_ok=True)
        frames = {p: minutes[p.split("/")[0]] for p in pairs if p.split("/")[0] in minutes}
        eth, btc = minutes["ETH"], minutes["BTC"]
        frames["ETH/BTC"] = eth.assign(**{c: eth[c] / btc[c] for c in ("open", "close")}, high=eth["high"] / btc["low"],
                                       low=eth["low"] / btc["high"])
        for pair, m in frames.items():
            for tf in TIMEFRAMES:
                df = resample(m, tf)
                df.to_feather(data / f"{pair_to_filename(pair)}-{tf}.feather")
                if pair in pairs:
                    rows.append((ex, pair, tf, df["date"].iloc[0].to_pydatetime(), df["date"].iloc[-1].to_pydatetime(), len(df)))
    return rows


def scratch_env(repo: Path, results: Path, names: list[str] | None, exchanges: dict[str, list[str]]) -> dict:
    only = " ".join(names) if names is not None else ""
    return {**os.environ, "REPO": str(repo), "LAB_RESULTS": str(results), "FT_USER_DATA": str(repo / "user_data"),
            "DATABASE_URL": calibration.scratch_url(SCHEMA), "PYTHONPATH": str(REPO),
            "ANALYZE_EXCHANGES": " ".join(exchanges["backtest"]), "BIAS_EXCHANGE": exchanges["bias"][0],
            "REGIME_EXCHANGE": exchanges["backtest"][0], "ANALYZE_STRATEGIES": only, "ANALYZE_WORKERS": str(WORKERS),
            "ANALYZE_SHARDS": "1", "ANALYZE_BUDGET_S": "0", "ANALYZE_JOB": "", "JOB_COMPLETION_INDEX": "0",
            "WF_EXCHANGES": " ".join(exchanges["walkforward"]), "WF_STRATEGIES": only, "WF_LIMIT": "100000",
            "WF_FOLDS": "1", "WF_TRAIN_DAYS": str(TRAIN_DAYS), "WF_EPOCHS": str(EPOCHS), "WF_EPOCHS_ROI": str(EPOCHS),
            "WF_EARLY_STOP": "0", "WF_WORKERS": str(WORKERS), "WF_SHARDS": "1", "WF_JOB": "", "WF_BUDGET_S": "0"}


def step(name: str, code: str, env: dict, report: dict) -> int:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], env=env, cwd=REPO)
    report["stage_s"][name] = round(time.monotonic() - t0, 1)
    return p.returncode


def probe(spec: dict) -> dict:
    """In the scratch environment: each strategy's reproduce checks (one cached chunk and the fold per walk-forward
    venue, lab/crypto/reproduce.py's own checks) and the league's guard and load under the bot's config files."""
    from freqtrade.configuration import Configuration
    from freqtrade.resolvers import StrategyResolver

    from lab.crypto import analyze, league, reproduce, walkforward

    names, today = set(spec["names"]), dt.date.fromisoformat(spec["today"])
    bt, wf = analyze.Cli(), walkforward.Cli()
    rng = random.Random(0)
    out: dict = {"reproduce": {}, "paper": {}}
    with connect(autocommit=True) as conn:
        chunks: dict = {}
        for c in reproduce.chunk_sample(conn, rng, 10 ** 9, bt.version(), today):
            if c["strategy"] in names and not c["overlay"] and c["exchange"] in spec["reproduce"]:
                chunks.setdefault((c["strategy"], c["exchange"]), c)
        folds = {(f["strategy"], f["exchange"]): f for f in reproduce.fold_sample(conn, rng, 10 ** 9, wf.version())
                 if f["strategy"] in names and f["exchange"] in spec["reproduce"]}

        def check(kind, task):
            try:
                return reproduce.check_chunk(conn, bt, task) if kind == "chunk" else reproduce.check_fold(wf, task)
            except Exception as e:
                return {"failed": f"{type(e).__name__}: {e}"[:300]}

        jobs = [("chunk", k, c) for k, c in chunks.items()] + [("fold", k, f) for k, f in folds.items()]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for (kind, key, _), got in zip(jobs, pool.map(lambda j: check(j[0], j[2]), jobs)):
                out["reproduce"].setdefault(f"{key[0]}|{key[1]}", {})[kind] = got
    for ex in spec["paper"]:
        configs = [analyze.USER_DATA / "config.base.json", analyze.USER_DATA / "exchanges" / f"{ex}.json"]
        try:
            league.guard(configs)
            base = Configuration.from_files([str(p) for p in configs])
        except SystemExit:
            for n in names:
                out["paper"][f"{n}|{ex}"] = "the league guard refused the config"
            continue
        for n in sorted(names):
            try:
                StrategyResolver.load_strategy({**base, "strategy": n, "user_data_dir": analyze.USER_DATA,
                                                "recursive_strategy_search": True})
                out["paper"][f"{n}|{ex}"] = None
            except Exception as e:
                out["paper"][f"{n}|{ex}"] = f"{type(e).__name__}: {e}"[:300]
    return out


def cells(sc, names: list[str], fams: dict[str, str], ex: dict[str, list[str]], wf_report: dict, probed: dict) -> list[dict]:
    """One row per strategy, avenue and exchange, read from the scratch tables and the probe."""
    from lab.crypto import analyze

    found = {n: (f, err) for n, f, err in sc.execute("SELECT name, file, load_error FROM strategies")}
    chunks: dict = {}
    for s, e, ok, err, attempts, trades in sc.execute(
            "SELECT strategy, exchange, ok, error, attempts, trades FROM backtest_chunks WHERE overlay = '' ORDER BY chunk_start"):
        chunks.setdefault((s, e), []).append((ok, err, attempts, trades))
    bias = {(s, k): (b, err) for s, k, b, err in sc.execute("SELECT strategy, kind, has_bias, error FROM bias_checks")}
    folds = {(s, e): (ok, err, bt, tt) for s, e, ok, err, bt, tt in sc.execute(
        "SELECT strategy, exchange, ok, error, base_trades, tuned_trades FROM walkforward")}
    windows = {(s, e) for s, e in sc.execute("SELECT strategy, exchange FROM strategy_windows "
                                             "WHERE win = '365d' AND expectancy_lo IS NOT NULL")}
    ready = {(s, v): (r, p) for s, v, r, p in sc.execute("SELECT strategy, venue, ready, passed FROM readiness "
                                                          "WHERE market = 'crypto'")}
    booked = {(s, e) for s, e in sc.execute("SELECT DISTINCT strategy, exchange FROM book")}
    candidates = set(wf_report.get("candidates") or [])
    skip_fams = set(cohorts.load()["strategies"]["skip_families"])
    out = []

    def add(name, avenue, exchange, status, reason):
        out.append({"strategy": name, "family": fams.get(name), "exchange": exchange, "avenue": avenue,
                    "status": status, "reason": reason})

    for name in names:
        file, load_error = found.get(name, (None, None))
        gone = name not in found
        not_run = ("in lab/crypto/families.toml, but no file under user_data/strategies has the class" if gone
                   else "needs the trader-ml image" if load_error == analyze.NEEDS_ML
                   else f"does not load: {load_error}" if load_error else None)
        biased = bias.get((name, "lookahead"), (None, None))[0] is True
        for avenue in AVENUES:
            for e in ex[avenue]:
                if not_run:
                    status = "skip" if load_error == analyze.NEEDS_ML else "fail" if avenue == "backtest" else "skip"
                    add(name, avenue, e, status, not_run)
                    continue
                if avenue == "backtest":
                    rows = chunks.get((name, e), [])
                    failed = [err for ok, err, attempts, _ in rows if not ok and attempts < analyze.MAX_ATTEMPTS]
                    if name not in fams:
                        add(name, avenue, e, "fail", "no line in lab/crypto/families.toml, so /coverage shows it untagged")
                    elif failed:
                        add(name, avenue, e, "fail", f"{len(failed)} of {len(rows)} chunks failed: {failed[0]}"[:300])
                    elif not rows:
                        add(name, avenue, e, "fail", "no backtest chunk ran")
                    elif all(not ok for ok, *_ in rows):
                        add(name, avenue, e, "skip", f"no chunk has candles to run: {rows[0][1]}"[:300])
                    else:
                        add(name, avenue, e, "ok", f"{len(rows)} chunks, {sum(t or 0 for *_, t in rows)} trades")
                elif avenue == "bias":
                    kinds = ("lookahead",) if analyze.freqai_fragment(file) else ("lookahead", "recursive")
                    found_bias = [(k, *bias[name, k]) for k in kinds if (name, k) in bias]
                    hit = [k for k, b, _ in found_bias if b]
                    errs = [f"{k}: {err}" for k, b, err in found_bias if b is None]
                    each = "; ".join(f"{k}: clean" if b is False else f"{k}: {err}" for k, b, err in found_bias)
                    if hit:
                        add(name, avenue, e, "fail", f"biased: {', '.join(hit)}")
                    elif len(found_bias) < len(kinds):
                        add(name, avenue, e, "fail", "not checked")
                    elif errs and all(NOTHING_TO_JUDGE.search(x) for x in errs):
                        add(name, avenue, e, "skip", each[:300])
                    elif errs:
                        add(name, avenue, e, "fail", each[:300])
                    else:
                        add(name, avenue, e, "ok", " and ".join(kinds) + " clean")
                elif avenue == "walkforward":
                    fold = folds.get((name, e))
                    if fold:
                        ok, err, bt, tt = fold
                        add(name, avenue, e, "ok" if ok else "fail",
                            f"base {bt} trades, tuned {tt}" if ok else f"fold failed: {err}"[:300])
                    elif name in candidates:
                        add(name, avenue, e, "fail", "a candidate, but no fold ran")
                    elif biased:
                        add(name, avenue, e, "skip", "not lookahead-clean, so not a candidate")
                    elif (name, e) not in windows:
                        add(name, avenue, e, "skip", "not a candidate: no 365-day expectancy low end (too few trades)")
                    else:
                        add(name, avenue, e, "fail", "not a candidate, with a 365-day window and clean bias")
                elif avenue == "reproduce":
                    got = probed["reproduce"].get(f"{name}|{e}", {})
                    bad = [f"{k}: {v['failed']}" if "failed" in v else f"{k}: {'; '.join(v['diff'])}"
                           for k, v in got.items() if "failed" in v or v.get("diff")]
                    if bad:
                        add(name, avenue, e, "fail", "; ".join(bad)[:300])
                    elif got:
                        add(name, avenue, e, "ok", f"{' and '.join(sorted(got))} identical on rerun")
                    else:
                        add(name, avenue, e, "skip", "nothing cached to rerun: no ok chunk on this venue")
                elif avenue == "confidence":
                    r = ready.get((name, e))
                    if r is None:
                        add(name, avenue, e, "fail" if folds.get((name, e), (False,))[0] else "skip",
                            "no readiness row" + ("" if folds.get((name, e), (False,))[0] else ": no walk-forward fold"))
                    else:
                        add(name, avenue, e, "ok", f"readiness row, {r[1]} gates passed"
                            + (", in the book" if (name, e) in booked else ", not in the book (no scored fold)"))
                else:
                    fam = fams.get(name)
                    why = probed["paper"].get(f"{name}|{e}", "not probed")
                    if fam in skip_fams:
                        add(name, avenue, e, "skip", f"the strategies cohort skips the {fam} family")
                    elif biased:
                        add(name, avenue, e, "skip", "not lookahead-clean, so the strategies cohort leaves it out")
                    elif why:
                        add(name, avenue, e, "fail", why)
                    else:
                        add(name, avenue, e, "ok", "guard passes and the strategy loads under the bot config")
    return out


def expected(report: dict) -> int:
    """Cells a complete matrix has: every strategy on every avenue's exchanges."""
    return report["strategies"] * sum(len(v) for v in report["exchanges"].values())


def summary(rows: list[dict]) -> dict:
    out: dict = {a: {"ok": 0, "skip": 0, "fail": 0} for a in AVENUES}
    for r in rows:
        out[r["avenue"]][r["status"]] += 1
    return out


def run(conn, report: dict, names: list[str] | None = None, fams: dict[str, str] | None = None,
        extra: tuple[Path, ...] = ()) -> list[dict]:
    """The matrix over names (None: the whole book), recorded in report; returns the cells."""
    from lab.crypto import analyze

    fams = cohorts.families() if fams is None else fams
    ex = venues()
    today = dt.date(dt.datetime.now(dt.timezone.utc).year, 1, 1)
    since = dt.datetime(today.year - 1, 1, 1, tzinfo=dt.timezone.utc)
    report.update(exchanges=ex, today=str(today), stage_s={})
    if names is None:
        names = book(analyze.Cli().discover(), fams) + [f.stem for f in extra]
    report["strategies"] = len(names)
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        calibration.migrate_scratch(SCHEMA)
        with tempfile.TemporaryDirectory(prefix="flow-", dir=WORK) as d:
            all_ex = sorted({e for v in ex.values() for e in v})
            repo, results = write_repo(Path(d) / "repo", all_ex, extra), Path(d) / "lab"
            t0 = time.monotonic()
            start, end = span()
            cover = write_candles(repo, results, all_ex, start, end)
            report["stage_s"]["candles"] = round(time.monotonic() - t0, 1)
            with connect(autocommit=True, dsn=calibration.scratch_url(SCHEMA)) as sc:
                with sc.cursor() as cur:
                    cur.executemany("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, "
                                    "oldest_reached) VALUES (%s, %s, %s, %s, %s, %s, true)",
                                    [(e, p, tf, max(first, since), last, n) for e, p, tf, first, last, n in cover])
                sc.execute("TRUNCATE calls CASCADE")  # production's seeded calls have no prices in here (calibration)
            env = scratch_env(repo, results, names, ex)
            report["analysis_exit"] = step("analysis", f"import sys, datetime; from lab.crypto import analyze; "
                                           f"sys.exit(analyze.main(today=datetime.date.fromisoformat('{today}')))", env, report)
            report["walkforward_exit"] = step("walkforward", f"import sys, datetime; from lab.crypto import walkforward; "
                                              f"sys.exit(walkforward.main(today=datetime.date.fromisoformat('{today}')))", env, report)
            spec = Path(d) / "probe.json"
            spec.write_text(json.dumps({"names": names, "today": str(today), "reproduce": ex["reproduce"], "paper": ex["paper"]}))
            t0 = time.monotonic()
            p = subprocess.run([sys.executable, "-m", "lab.crypto.flow_matrix", "probe", str(spec)], env=env, cwd=REPO,
                               capture_output=True, text=True)
            report["stage_s"]["probe"] = round(time.monotonic() - t0, 1)
            line = next((x for x in reversed(p.stdout.splitlines()) if x.startswith("FLOW_PROBE ")), None)
            if line is None:
                report["probe_error"] = (p.stderr or p.stdout)[-1000:]
            probed = json.loads(line.removeprefix("FLOW_PROBE ")) if line else {"reproduce": {}, "paper": {}}
        with connect(autocommit=True, dsn=calibration.scratch_url(SCHEMA)) as sc:
            wf = sc.execute("SELECT raw FROM runs WHERE kind = 'walkforward' ORDER BY id DESC LIMIT 1").fetchone()
            rows = cells(sc, names, fams, ex, wf[0] if wf else {}, probed)
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    report["summary"] = summary(rows)
    report["cells"] = rows
    return rows


def main(names: list[str] | None = None, fams: dict[str, str] | None = None, extra: tuple[Path, ...] = ()) -> int:
    """Records the run as kind 'flow_matrix'. ok means every cell has a status; failing cells are its findings."""
    with connect(autocommit=True) as conn:
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('flow_matrix', %s, '{}', now()) "
                              "RETURNING id", (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0]
        report: dict = {}
        ok = False
        try:
            rows = run(conn, report, names, fams, extra)
            ok = len(rows) == expected(report)
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s", (Jsonb(report), ok, run_id))
        print(f"flow_matrix: {report.get('strategies')} strategies, {json.dumps(report.get('summary'))}", flush=True)
        for r in report.get("cells", []):
            if r["status"] == "fail":
                print(f"flow_matrix: fail {r['strategy']} {r['avenue']} {r['exchange']}: {r['reason']}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["probe"]:
        print("FLOW_PROBE " + json.dumps(probe(json.loads(Path(sys.argv[2]).read_text()))), flush=True)
        sys.exit(0)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", action="store_true", help="one strategy per family, as the CI gate runs")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    sys.exit(main(sample(cohorts.families()) if a.sample else a.names or None))
