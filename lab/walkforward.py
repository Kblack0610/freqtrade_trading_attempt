#!/usr/bin/env python3
"""Weekly walk-forward test: do hyperopt's parameters hold on months it never saw?

Candidates, lookahead-clean, loadable and not FreqAI, up to WF_LIMIT: the league's strategies, then the open promote and
watch suggestions, then the best 365-day expectancy low end on each venue in turn. Each runs on every venue in
WF_EXCHANGES whose candles for its timeframe are fully backfilled (coinbase is left out: its 2.4% round trip
leaves nothing to tune). For each of the last WF_FOLDS whole calendar months (a trade the backtest force-closes at a span's end
straddles the fold boundary, so `stats` purges it from every span's result):
- the month is backtested with the strategy as it is (base);
- hyperopt tunes it on the WF_TRAIN_DAYS before the month, with a seeded random state, stopping WF_EMBARGO_DAYS
  short of the month so no training trade is still open when the test begins (embargo);
- the month is backtested again with the tuned parameters, and so is the training span (in sample).
How a fold tunes (`tuning`): a strategy whose source declares no hyperopt parameter has only ROI and stoploss to
tune, so it gets WF_EPOCHS_ROI epochs instead of WF_EPOCHS. Every fold stops after WF_EARLY_STOP epochs without a
better loss. The optuna sampler is an A/B: each fold draws one of WF_SAMPLERS from its seed, so every strategy has
folds in each arm, and the run report's `arms` compares their hyperopt seconds and out-of-sample uplift. Each arm's
tuned parameters enter the trial register like any other (lab/confidence.py). A fold row keeps its hyperopt and
backtest seconds apart, so the cost of each step is measured, not guessed.

A fold is cached by strategy file, configs, Freqtrade version, dates, hyperopt settings and a digest of the
candles from its training start to its test end, so a weekly run adds the newest month and whatever a changed
file, a changed candle or a new candidate needs. Folds share a memory budget
(WF_MEMORY_GB) by their estimated size, like analyze's chunks, so wide venues do not all hyperopt at once.

A FreqAI strategy (analyze.freqai_fragment) is left out: hyperopt over a fold's training span would fit a model per pair
for every 7-day window in it, each fold anew, far past WF_BUDGET_S.

Hyperopt runs on a copy of the strategy's folder in a temporary userdir, so its params file never lands
next to the real strategy, where every nightly backtest would load it.

Rows go to `walkforward`, and each test-month trade, dated, to `walkforward_trades`; lab/suggest.py turns them into retune suggestions and runs at the end.
A run starts no fold after WF_BUDGET_S seconds (0: no limit), so a backlog longer than the job's deadline still ends in
a recorded run with its post steps; the folds it left (`folds_left`) run next time, skipped by cache_key once stored.
A run can be split over WF_SHARDS pods (a Kubernetes Indexed Job, each pod's JOB_COMPLETION_INDEX its shard): each
strategy x venue belongs to one shard by a hash of its names, so every pod plans the same split. Each shard records a
`walkforward_shard` row named WF_JOB#index; the shard that finishes last (under an advisory lock) runs the post steps
and records the run's `walkforward` row, its counts summed over the shards. A shard killed before it records leaves
the run without that row, so TraderRunStale tells; its folds run next time.
Exits 1 when more than a tenth of the folds this shard ran failed, or on a crash.
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from psycopg.types.json import Jsonb

from lab import analyze, book, confidence, horizon, metrics, mixtures, robustness, scorecard, suggest, universe, vol_target
from lab.db import connect

EXCHANGES = os.environ.get("WF_EXCHANGES", "binanceus").split()
FOLDS = int(os.environ.get("WF_FOLDS", "6"))
TRAIN_DAYS = int(os.environ.get("WF_TRAIN_DAYS", "180"))
EPOCHS = int(os.environ.get("WF_EPOCHS", "150"))
EPOCHS_ROI = int(os.environ.get("WF_EPOCHS_ROI", "60"))  # a strategy with no parameters of its own: ROI and stoploss only
EARLY_STOP = int(os.environ.get("WF_EARLY_STOP", "40"))  # epochs without a better loss; 0: off
SAMPLERS = os.environ.get("WF_SAMPLERS", "NSGAIIISampler TPESampler").split()  # the A/B arms, picked by fold seed
LOSS = os.environ.get("WF_LOSS", "SharpeHyperOptLossDaily")
MIN_TRADES = int(os.environ.get("WF_MIN_TRADES", "10"))  # hyperopt ignores an epoch with fewer training trades
LIMIT = int(os.environ.get("WF_LIMIT", "16"))
WORKERS = int(os.environ.get("WF_WORKERS", "6"))
TIMEOUT = int(os.environ.get("WF_TIMEOUT", "10800"))
MEMORY_GB = float(os.environ.get("WF_MEMORY_GB", "10"))
BUDGET_S = float(os.environ.get("WF_BUDGET_S", "0"))  # under the job's deadline by WF_TIMEOUT and the post steps
EMBARGO_DAYS = int(os.environ.get("WF_EMBARGO_DAYS", "7"))  # a gap longer than any trade's hold, before each test month
HYPEROPT_LOAD = 2  # hyperopt keeps the analysed frame and each epoch's backtest copy
ONLY = set(os.environ.get("WF_STRATEGIES", "").split())
SHARDS = int(os.environ.get("WF_SHARDS", "1"))
SHARD = int(os.environ.get("JOB_COMPLETION_INDEX", "0"))
JOB = os.environ.get("WF_JOB", "")  # the Job's name, shared by its shards; a single run names itself by its start
MAX_ATTEMPTS = analyze.MAX_ATTEMPTS
FAIL_SHARE = analyze.FAIL_SHARE


def folds(today: dt.date, n: int | None = None, train_days: int | None = None,
          lockbox: dt.date | None = None) -> list[tuple[dt.date, dt.date, dt.date]]:
    """(train_start, test_start, test_end) for the last n (FOLDS) whole months before the lockbox, oldest first."""
    n, train_days = n or FOLDS, train_days or TRAIN_DAYS
    out, end = [], min(today.replace(day=1), lockbox or today)
    for _ in range(n):
        start = (end - dt.timedelta(days=1)).replace(day=1)
        out.append((start - dt.timedelta(days=train_days), start, end))
        end = start
    return out[::-1]


def train_end(test_start: dt.date) -> dt.date:
    """Where the training span stops: the embargo before the test month."""
    return test_start - dt.timedelta(days=EMBARGO_DAYS)


PARAMETER = re.compile(r"\b(?:Int|Decimal|Real|Categorical|Boolean)Parameter\s*\(")
IMPORT = re.compile(r"^\s*(?:from\s+\.?(\w+)\s+import|import\s+(\w+))", re.M)


def has_parameters(file: str) -> bool:
    """Whether the strategy, or a sibling module it imports, declares a hyperopt parameter. A file it cannot
    read counts as having them, so the doubt falls on the full search."""
    src = analyze.REPO / file
    try:
        text = src.read_text()
    except OSError:
        return True
    siblings = [src.parent / f"{a or b}.py" for a, b in IMPORT.findall(text)]
    return any(PARAMETER.search(t) for t in [text, *(f.read_text() for f in siblings if f.is_file())])


def tuning(file: str, seed: int) -> dict:
    """How a fold's hyperopt runs: its spaces, epochs, early stop and sampler arm."""
    full = has_parameters(file)
    return {"spaces": "default" if full else "roi stoploss", "epochs": EPOCHS if full else EPOCHS_ROI,
            "early_stop": EARLY_STOP, "sampler": SAMPLERS[seed % len(SAMPLERS)]}


def fold_key(file_hash: str, exchange: str, fold: tuple, ft_version: str, candles: str, tune: dict) -> str:
    return analyze.sha256(json.dumps([file_hash, analyze.config_digest(exchange), ft_version, exchange,
                                      [str(d) for d in fold], tune, LOSS, MIN_TRADES, EMBARGO_DAYS, candles,
                                      robustness.settings(suggest.load_config()["confidence"])]).encode())


def sampler_override(strategy: str, sampler: str) -> str:
    """Source appended to the temporary copy of a strategy so hyperopt uses the fold's sampler. freqtrade asks
    the strategy's HyperOpt class for generate_estimator (hyperopt_auto.py), so only that method is set."""
    return (f"\n\n_wf_sampler = staticmethod(lambda dimensions, **kwargs: {sampler!r})\n"
            f"if not hasattr({strategy}, 'HyperOpt'):\n    {strategy}.HyperOpt = type('HyperOpt', (), {{}})\n"
            f"{strategy}.HyperOpt.generate_estimator = _wf_sampler\n")


def fold_candles(exchange: str, timeframe: str, fold: tuple, days: dict) -> str:
    """One digest for the native and detail candles, shared by planning and reproduction."""
    digests = []
    for tf in (timeframe, suggest.load_config()["confidence"]["detail_timeframe"]):
        if (exchange, tf) not in days:
            days[exchange, tf] = analyze.candle_digests(exchange, tf, by="day")
        digests.append(analyze.span_digest(days[exchange, tf], fold[0], fold[2]))
    return analyze.sha256("".join(digests).encode())


class Runner(Protocol):
    def version(self) -> str: ...
    def fold(self, strategy: str, file: str, exchange: str, fold: tuple, seed: int) -> dict: ...


def stats(res: dict) -> dict:
    """A backtest result as the trades, total return and per-trade profit ratios a fold row keeps. Trades the
    backtest force-closed at the span's end are dropped: they straddle the fold boundary."""
    t = res["trades"]
    if len(t) and "exit_reason" in t:
        t = t[t["exit_reason"] != "force_exit"]
    ret = [float(x) for x in t["profit_ratio"]] if len(t) else []
    profit = float(t["profit_abs"].sum()) / res["start_balance"] if len(t) else 0.0
    dated = [(str(r.pair), r.open_date.to_pydatetime(), r.close_date.to_pydatetime(), float(r.profit_ratio))
             for r in t.itertuples()] if len(t) and "pair" in t else []
    return {"trades": len(ret), "profit": profit,
            "expectancy": sum(ret) / len(ret) if ret else None, "returns": ret, "dated": dated}


EARLY_STOPPED = re.compile(r"Early stopping after (\d+) epochs")


class Cli:
    def __init__(self):
        self.bt = analyze.Cli()

    def version(self) -> str:
        return self.bt.version()

    def fold(self, strategy: str, file: str, exchange: str, fold: tuple, seed: int) -> dict:
        train_start, test_start, test_end = fold
        tune = tuning(file, seed)
        t0, hyperopt_s = time.monotonic(), None

        def took(r: dict) -> dict:
            total = time.monotonic() - t0
            return {**r, "duration_s": total, "hyperopt_s": hyperopt_s,
                    "backtest_s": total - (hyperopt_s or 0)}  # everything but hyperopt: the backtests

        src = analyze.REPO / file
        with tempfile.TemporaryDirectory(prefix="wf-") as d:
            strat = Path(d) / "strategies"
            strat.mkdir()
            for f in [*src.parent.glob("*.py"), *src.parent.glob("*.json")]:  # siblings it may import, its params
                shutil.copy2(f, strat / f.name)
            params_file = strat / f"{src.stem}.json"
            with open(strat / src.name, "a") as f:
                f.write(sampler_override(strategy, tune["sampler"]))
            base = self.bt.backtest(strategy, exchange, test_start, test_end, strategy_path=strat)
            if not base["ok"]:
                return took({"ok": False, "no_data": base.get("no_data", False), "error": f"base: {base['error']}"})
            before = params_file.stat().st_mtime_ns if params_file.exists() else None
            cmd = ["freqtrade", "hyperopt", "--userdir", d,
                   *[a for p in analyze.config_files(exchange) for a in ("-c", str(p))],
                   "--datadir", str(analyze.RESULTS / "data" / exchange), "--strategy", strategy,
                   "--strategy-path", str(strat), "--timerange", f"{train_start:%Y%m%d}-{train_end(test_start):%Y%m%d}",
                   "--hyperopt-loss", LOSS, "--spaces", *tune["spaces"].split(), "--ignore-missing-spaces",
                   "-e", str(tune["epochs"]), "--early-stop", str(tune["early_stop"]),
                   "-j", "1", "--random-state", str(seed), "--min-trades", str(MIN_TRADES)]
            h0 = time.monotonic()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                return took({"ok": False, "error": f"hyperopt timed out after {TIMEOUT}s"})
            finally:
                hyperopt_s = time.monotonic() - h0
            if p.returncode:
                return took({"ok": False, "error": f"hyperopt exit {p.returncode}: {analyze._errors(p.stdout + p.stderr)}"})
            stopped = EARLY_STOPPED.search(p.stdout + p.stderr)
            out = {"ok": True, "error": None, "base": stats(base), "params": None,
                   "epochs_run": int(stopped.group(1)) if stopped else tune["epochs"]}
            if not params_file.exists() or params_file.stat().st_mtime_ns == before:
                return took({**out, "error": f"no hyperopt epoch reached {MIN_TRADES} training trades"})
            out["params"] = json.loads(params_file.read_text())["params"]
            for side, (a, b) in (("tuned", (test_start, test_end)), ("is", (train_start, train_end(test_start)))):
                r = self.bt.backtest(strategy, exchange, a, b, strategy_path=strat)
                if not r["ok"]:
                    return took({"ok": False, "error": f"{side}: {r['error']}"})
                out[side] = stats(r)
            out["robustness"] = robustness.run(self.bt, strategy, strat, params_file, exchange,
                                               test_start, test_end, suggest.load_config()["confidence"], stats)
        return took(out)


def shard_of(strategy: str, exchange: str, shards: int) -> int:
    """The shard a strategy x venue runs in: stable across pods and runs, so each plans the same split."""
    return int(analyze.sha256(f"{strategy} {exchange}".encode())[:8], 16) % shards


def candidates(conn) -> list[dict]:
    """League strategies first, then promote and watch suggestions by score, then each venue's best 365-day
    expectancy low end, one venue after another; loadable and lookahead-clean."""
    rows = conn.execute("""
        WITH best AS (
            SELECT strategy, min(rn) AS rn, max(expectancy_lo) AS lo FROM (
                SELECT strategy, expectancy_lo,
                       row_number() OVER (PARTITION BY exchange ORDER BY expectancy_lo DESC, strategy) AS rn
                FROM strategy_windows
                WHERE win = '365d' AND exchange = ANY(%s) AND expectancy_lo IS NOT NULL) w
            GROUP BY strategy)
        SELECT s.name, s.file, s.file_hash, s.timeframe,
               min(CASE WHEN b.name IS NOT NULL THEN 0 WHEN g.kind = 'promote' THEN 1
                        WHEN g.kind = 'watch' THEN 2 ELSE 3 END) AS tier,
               max(g.score), min(w.rn), max(w.lo)
        FROM strategies s
        LEFT JOIN bots b ON b.strategy = s.name AND b.last_seen >= now() - interval '1 day'
        LEFT JOIN suggestions g ON g.strategy = s.name AND g.kind IN ('promote', 'watch')
        LEFT JOIN best w ON w.strategy = s.name
        WHERE s.load_error IS NULL AND (b.name IS NOT NULL OR g.kind IS NOT NULL OR w.strategy IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM bias_checks c WHERE c.strategy = s.name AND c.kind = 'lookahead' AND c.has_bias)
        GROUP BY 1, 2, 3, 4 ORDER BY 5, 6 DESC NULLS LAST, 7 NULLS LAST, 8 DESC NULLS LAST, 1""",
        (EXCHANGES,)).fetchall()
    out = [{"name": n, "file": f, "file_hash": h, "timeframe": tf} for n, f, h, tf, *_ in rows
           if (not ONLY or n in ONLY) and not analyze.freqai_fragment(f)]
    return out[:LIMIT]


def plan(conn, found: list[dict], first: dict, today: dt.date, ft_version: str) -> tuple[list[dict], int, list[str]]:
    """The folds to run, how many are cached, and why a strategy x venue or a fold is skipped. A venue runs once
    its candles are fully backfilled (analyze.coverage), and a fold only when its training span has candles."""
    done = {(s, e, f): (k, ok, n) for s, e, f, k, ok, n in conn.execute(
        "SELECT strategy, exchange, fold_start, cache_key, ok, attempts FROM walkforward")}
    todo, cached, skipped = [], 0, []
    lockbox = suggest.load_config()["lockbox"]["start"]
    days: dict[tuple[str, str], dict] = {}
    for s in found:
        for ex in EXCHANGES:
            if shard_of(s["name"], ex, SHARDS) != SHARD:
                continue
            since = first.get((ex, s["timeframe"]))
            if since is None:
                skipped.append(f"{s['name']} on {ex}: {s['timeframe']} not fully backfilled")
                continue
            for fold in folds(today, lockbox=lockbox):
                if fold[0] < since.date():
                    skipped.append(f"{s['name']} on {ex} {fold[1]:%Y-%m}: training starts before its first candle")
                    continue
                candles = fold_candles(ex, s["timeframe"], fold, days)
                seed = metrics.seed_of(s["name"], ex, fold[1])
                tune = tuning(s["file"], seed)
                key = fold_key(s["file_hash"], ex, fold, ft_version, candles, tune)
                prior = done.get((s["name"], ex, fold[1]))
                if prior and prior[0] == key and (prior[1] or prior[2] >= MAX_ATTEMPTS):
                    cached += 1
                    continue
                todo.append({**s, "exchange": ex, "fold": fold, "key": key, "candles": candles, "seed": seed, "tune": tune,
                             "attempts": prior[2] if prior and prior[0] == key and not prior[1] else 0})
    todo.sort(key=lambda t: (-t["fold"][1].toordinal(), t["name"], t["exchange"]))  # the newest month first
    return todo, cached, skipped


def store(conn, task: dict, res: dict) -> None:
    side = lambda k: res.get(k) or {}  # noqa: E731
    attempts = MAX_ATTEMPTS if res.get("no_data") else task["attempts"] + 1 if not res["ok"] else 1
    train_start, fold_start, test_end = task["fold"]
    conn.execute("""
        INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, attempts, error,
            epochs, loss, params, is_trades, is_profit, base_trades, base_profit, base_expectancy, base_returns,
            tuned_trades, tuned_profit, tuned_expectancy, tuned_returns, duration_s, revision, config_digest, candles, seed,
            robustness, hyperopt_s, backtest_s, sampler, spaces, epochs_run, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (strategy, exchange, fold_start) DO UPDATE SET train_start = EXCLUDED.train_start,
            test_end = EXCLUDED.test_end, cache_key = EXCLUDED.cache_key, ok = EXCLUDED.ok, attempts = EXCLUDED.attempts,
            error = EXCLUDED.error, epochs = EXCLUDED.epochs, loss = EXCLUDED.loss, params = EXCLUDED.params,
            is_trades = EXCLUDED.is_trades, is_profit = EXCLUDED.is_profit, base_trades = EXCLUDED.base_trades,
            base_profit = EXCLUDED.base_profit, base_expectancy = EXCLUDED.base_expectancy,
            base_returns = EXCLUDED.base_returns, tuned_trades = EXCLUDED.tuned_trades,
            tuned_profit = EXCLUDED.tuned_profit, tuned_expectancy = EXCLUDED.tuned_expectancy,
            tuned_returns = EXCLUDED.tuned_returns, duration_s = EXCLUDED.duration_s, revision = EXCLUDED.revision,
            config_digest = EXCLUDED.config_digest, candles = EXCLUDED.candles, seed = EXCLUDED.seed, robustness = EXCLUDED.robustness,
            hyperopt_s = EXCLUDED.hyperopt_s, backtest_s = EXCLUDED.backtest_s, sampler = EXCLUDED.sampler,
            spaces = EXCLUDED.spaces, epochs_run = EXCLUDED.epochs_run, computed_at = now()""",
        (task["name"], task["exchange"], fold_start, train_start, test_end, task["key"], res["ok"], attempts, res.get("error"),
         task["tune"]["epochs"], LOSS, Jsonb(res["params"]) if res.get("params") else None,
         side("is").get("trades"), side("is").get("profit"),
         side("base").get("trades"), side("base").get("profit"), side("base").get("expectancy"), side("base").get("returns"),
         side("tuned").get("trades"), side("tuned").get("profit"), side("tuned").get("expectancy"),
         side("tuned").get("returns"), res.get("duration_s"), analyze.REVISION, analyze.config_digest(task["exchange"]),
         task["candles"], task["seed"], Jsonb({**res["robustness"], "_settings": robustness.settings(suggest.load_config()["confidence"])})
         if res.get("robustness") is not None else None,
         res.get("hyperopt_s"), res.get("backtest_s"), task["tune"]["sampler"], task["tune"]["spaces"], res.get("epochs_run")))
    conn.execute("DELETE FROM walkforward_trades WHERE strategy = %s AND exchange = %s AND fold_start = %s",
                 (task["name"], task["exchange"], fold_start))
    rows = [(task["name"], task["exchange"], v, fold_start, *t) for v in ("base", "tuned") for t in side(v).get("dated", [])]
    if rows:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO walkforward_trades (strategy, exchange, variant, fold_start, pair, open_date, "
                            "close_date, profit_ratio) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows)


def arms(conn) -> list[dict]:
    """The sampler A/B so far: per arm and search, the stored folds, their mean hyperopt seconds, epochs run, and
    out-of-sample uplift (tuned minus base expectancy). Judge an arm on the uplift, never on the in-sample score."""
    rows = conn.execute("""
        SELECT sampler, spaces, count(*), avg(hyperopt_s), avg(epochs_run)::float8,
               avg(tuned_expectancy - base_expectancy) FILTER (WHERE tuned_expectancy IS NOT NULL
                                                             AND base_expectancy IS NOT NULL)
        FROM walkforward WHERE ok AND sampler IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    return [{"sampler": s, "spaces": sp, "folds": n, "hyperopt_s": h, "epochs_run": e, "oos_uplift": u}
            for s, sp, n, h, e, u in rows]


def run_folds(conn, runner: Runner, today: dt.date, report: dict) -> bool:
    """This shard's folds, stored as each finishes; True unless more than FAIL_SHARE of them failed."""
    found = candidates(conn)
    todo, cached, skipped = plan(conn, found, analyze.coverage(conn), today, runner.version())
    report.update(candidates=[s["name"] for s in found], folds_run=len(todo), folds_cached=cached,
                  skipped=skipped[:100])
    print(f"walkforward: shard {SHARD + 1}/{SHARDS}: {len(found)} candidates on {' '.join(EXCHANGES)}, {len(todo)} folds "
          f"to run, {cached} cached, {len(skipped)} skipped, {WORKERS} workers", flush=True)
    failed, done, failures = 0, 0, []
    budget = analyze.Budget(MEMORY_GB)
    stop_at = time.monotonic() + BUDGET_S if BUDGET_S else None
    left = 0

    def run(t):
        gb = max(analyze.task_gb(t["exchange"], t["timeframe"], TRAIN_DAYS + 31, HYPEROPT_LOAD),
                 analyze.task_gb(t["exchange"], suggest.load_config()["confidence"]["detail_timeframe"], 31))
        with budget.hold(gb):
            if stop_at is not None and time.monotonic() >= stop_at:
                return None  # out of time: left for the next run
            return runner.fold(t["name"], t["file"], t["exchange"], t["fold"], t["seed"])

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(run, t): t for t in todo}
        try:
            for fut in as_completed(futs):
                task = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # one fold's runner bug must not lose the others
                    res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if res is None:
                    left += 1
                    continue
                store(conn, task, res)
                done += 1
                if not res["ok"] and not res.get("no_data"):
                    failed += 1
                    failures.append(f"{task['name']} {task['exchange']} {task['fold'][1]:%Y-%m}: {res['error']}"[:300])
                print(f"walkforward: {done}/{len(todo)} folds, {failed} failed", flush=True)
        except BaseException:
            pool.shutdown(cancel_futures=True)
            raise
    if left:
        print(f"walkforward: budget of {BUDGET_S:.0f}s spent, {left} folds left for the next run", flush=True)
    report.update(folds_failed=failed, folds_left=left, failures=failures[:100])
    return failed <= FAIL_SHARE * max(done, 1)


def close_shard(conn, shard_id: int, job: str, report: dict, ok: bool) -> list[tuple] | None:
    """Records this shard; when it is the last of the job's shards to finish, the finished shards' (started_at,
    ok, raw), for the run's row. The advisory lock makes exactly one shard the last."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"walkforward:{job}",))
        conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s", (Jsonb(report), ok, shard_id))
        rows = conn.execute("SELECT started_at, ok, raw FROM runs WHERE kind = 'walkforward_shard' "
                            "AND split_part(name, '#', 1) = %s AND finished_at IS NOT NULL", (job,)).fetchall()
    return rows if len(rows) >= SHARDS else None


def finish(conn, job: str, shards: list[tuple]) -> bool:
    """The post steps, once per run, and the run's `walkforward` row: the shards' counts summed, their lists joined."""
    raws = [r for _, _, r in shards]
    report: dict = {k: raws[0].get(k) for k in ("exchanges", "universe", "folds", "train_days", "epochs",
                                                 "epochs_roi", "early_stop", "samplers", "loss")}
    report["shards"] = len(raws)
    for k in ("folds_run", "folds_cached", "folds_failed", "folds_left"):
        report[k] = sum(r.get(k) or 0 for r in raws)
    report["candidates"] = list(dict.fromkeys(x for r in raws for x in r.get("candidates") or []))
    for k in ("skipped", "failures"):
        report[k] = [x for r in raws for x in r.get(k) or []][:100]
    errors = [r["error"] for r in raws if r.get("error")]
    if errors:
        report["error"] = "; ".join(errors)[:1000]
    ok = all(o for _, o, _ in shards)
    try:
        report["arms"] = arms(conn)
        report.update(vol_target.run(conn))
        report.update(suggest.run(conn))
        report.update(confidence.run(conn))
        report.update(book.run(conn))
        report.update(mixtures.run(conn))
        report.update(scorecard.run(conn))
        report.update(horizon.run(conn))
    except Exception as e:
        report["error"] = f"{report.get('error', '')}; post: {type(e).__name__}: {e}".lstrip("; ")
        traceback.print_exc()
        ok = False
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('walkforward', %s, %s, %s, "
                 "now(), %s)", (job, Jsonb(report), min(t for t, _, _ in shards), ok))
    print(f"walkforward: done: {json.dumps({k: v for k, v in report.items() if k not in ('failures', 'skipped')})}",
          flush=True)
    return ok


def main(runner: Runner | None = None, today: dt.date | None = None) -> int:
    runner = runner or Cli()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    with connect(autocommit=True) as conn:
        started = dt.datetime.now(dt.timezone.utc)
        job = JOB or f"{started:%Y-%m-%d %H:%M:%S.%f}"
        shard_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('walkforward_shard', %s, '{}', %s) "
                                "RETURNING id", (f"{job}#{SHARD}", started)).fetchone()[0]
        report: dict = {"exchanges": EXCHANGES, "universe": universe.describe_all(analyze.USER_DATA, EXCHANGES), "folds": FOLDS,
                        "train_days": TRAIN_DAYS, "epochs": EPOCHS, "epochs_roi": EPOCHS_ROI, "early_stop": EARLY_STOP,
                        "samplers": SAMPLERS, "loss": LOSS, "shard": SHARD, "shards": SHARDS}
        ok = False
        try:
            ok = run_folds(conn, runner, today, report)
        except Exception as e:  # recorded, so the last shard still finishes the run over the others' folds
            report["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        shards = close_shard(conn, shard_id, job, report, ok)
        if shards is not None:
            ok = finish(conn, job, shards) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
