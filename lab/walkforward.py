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
Exits 1 when more than a tenth of the folds run this time failed, or on a crash.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


def fold_key(file_hash: str, exchange: str, fold: tuple, ft_version: str, candles: str) -> str:
    return analyze.sha256(json.dumps([file_hash, analyze.config_digest(exchange), ft_version, exchange,
                                      [str(d) for d in fold], EPOCHS, LOSS, MIN_TRADES, EMBARGO_DAYS, candles,
                                      robustness.settings(suggest.load_config()["confidence"])]).encode())


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


class Cli:
    def __init__(self):
        self.bt = analyze.Cli()

    def version(self) -> str:
        return self.bt.version()

    def fold(self, strategy: str, file: str, exchange: str, fold: tuple, seed: int) -> dict:
        train_start, test_start, test_end = fold
        t0 = time.monotonic()
        took = lambda r: {**r, "duration_s": time.monotonic() - t0}  # noqa: E731
        src = analyze.REPO / file
        with tempfile.TemporaryDirectory(prefix="wf-") as d:
            strat = Path(d) / "strategies"
            strat.mkdir()
            for f in [*src.parent.glob("*.py"), *src.parent.glob("*.json")]:  # siblings it may import, its params
                shutil.copy2(f, strat / f.name)
            params_file = strat / f"{src.stem}.json"
            base = self.bt.backtest(strategy, exchange, test_start, test_end, strategy_path=strat)
            if not base["ok"]:
                return took({"ok": False, "no_data": base.get("no_data", False), "error": f"base: {base['error']}"})
            before = params_file.stat().st_mtime_ns if params_file.exists() else None
            cmd = ["freqtrade", "hyperopt", "--userdir", d,
                   *[a for p in analyze.config_files(exchange) for a in ("-c", str(p))],
                   "--datadir", str(analyze.RESULTS / "data" / exchange), "--strategy", strategy,
                   "--strategy-path", str(strat), "--timerange", f"{train_start:%Y%m%d}-{train_end(test_start):%Y%m%d}",
                   "--hyperopt-loss", LOSS, "--spaces", "default", "--ignore-missing-spaces", "-e", str(EPOCHS),
                   "-j", "1", "--random-state", str(seed), "--min-trades", str(MIN_TRADES)]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                return took({"ok": False, "error": f"hyperopt timed out after {TIMEOUT}s"})
            if p.returncode:
                return took({"ok": False, "error": f"hyperopt exit {p.returncode}: {analyze._errors(p.stdout + p.stderr)}"})
            out = {"ok": True, "error": None, "base": stats(base), "params": None}
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
            since = first.get((ex, s["timeframe"]))
            if since is None:
                skipped.append(f"{s['name']} on {ex}: {s['timeframe']} not fully backfilled")
                continue
            for fold in folds(today, lockbox=lockbox):
                if fold[0] < since.date():
                    skipped.append(f"{s['name']} on {ex} {fold[1]:%Y-%m}: training starts before its first candle")
                    continue
                candles = fold_candles(ex, s["timeframe"], fold, days)
                key = fold_key(s["file_hash"], ex, fold, ft_version, candles)
                prior = done.get((s["name"], ex, fold[1]))
                if prior and prior[0] == key and (prior[1] or prior[2] >= MAX_ATTEMPTS):
                    cached += 1
                    continue
                todo.append({**s, "exchange": ex, "fold": fold, "key": key, "candles": candles,
                             "seed": metrics.seed_of(s["name"], ex, fold[1]),
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
            robustness, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, now())
        ON CONFLICT (strategy, exchange, fold_start) DO UPDATE SET train_start = EXCLUDED.train_start,
            test_end = EXCLUDED.test_end, cache_key = EXCLUDED.cache_key, ok = EXCLUDED.ok, attempts = EXCLUDED.attempts,
            error = EXCLUDED.error, epochs = EXCLUDED.epochs, loss = EXCLUDED.loss, params = EXCLUDED.params,
            is_trades = EXCLUDED.is_trades, is_profit = EXCLUDED.is_profit, base_trades = EXCLUDED.base_trades,
            base_profit = EXCLUDED.base_profit, base_expectancy = EXCLUDED.base_expectancy,
            base_returns = EXCLUDED.base_returns, tuned_trades = EXCLUDED.tuned_trades,
            tuned_profit = EXCLUDED.tuned_profit, tuned_expectancy = EXCLUDED.tuned_expectancy,
            tuned_returns = EXCLUDED.tuned_returns, duration_s = EXCLUDED.duration_s, revision = EXCLUDED.revision,
            config_digest = EXCLUDED.config_digest, candles = EXCLUDED.candles, seed = EXCLUDED.seed, robustness = EXCLUDED.robustness, computed_at = now()""",
        (task["name"], task["exchange"], fold_start, train_start, test_end, task["key"], res["ok"], attempts, res.get("error"),
         EPOCHS, LOSS, Jsonb(res["params"]) if res.get("params") else None,
         side("is").get("trades"), side("is").get("profit"),
         side("base").get("trades"), side("base").get("profit"), side("base").get("expectancy"), side("base").get("returns"),
         side("tuned").get("trades"), side("tuned").get("profit"), side("tuned").get("expectancy"),
         side("tuned").get("returns"), res.get("duration_s"), analyze.REVISION, analyze.config_digest(task["exchange"]),
         task["candles"], task["seed"], Jsonb({**res["robustness"], "_settings": robustness.settings(suggest.load_config()["confidence"])})
         if res.get("robustness") is not None else None))
    conn.execute("DELETE FROM walkforward_trades WHERE strategy = %s AND exchange = %s AND fold_start = %s",
                 (task["name"], task["exchange"], fold_start))
    rows = [(task["name"], task["exchange"], v, fold_start, *t) for v in ("base", "tuned") for t in side(v).get("dated", [])]
    if rows:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO walkforward_trades (strategy, exchange, variant, fold_start, pair, open_date, "
                            "close_date, profit_ratio) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows)


def main(runner: Runner | None = None, today: dt.date | None = None) -> int:
    runner = runner or Cli()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    with connect(autocommit=True) as conn:
        started = dt.datetime.now(dt.timezone.utc)
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('walkforward', %s, '{}', %s) "
                              "RETURNING id", (started.strftime("%Y-%m-%d %H:%M:%S"), started)).fetchone()[0]
        report: dict = {"exchanges": EXCHANGES, "universe": universe.describe_all(analyze.USER_DATA, EXCHANGES), "folds": FOLDS,
                        "train_days": TRAIN_DAYS, "epochs": EPOCHS, "loss": LOSS}
        ok = False
        try:
            found = candidates(conn)
            todo, cached, skipped = plan(conn, found, analyze.coverage(conn), today, runner.version())
            report.update(candidates=[s["name"] for s in found], folds_run=len(todo), folds_cached=cached,
                          skipped=skipped[:100])
            print(f"walkforward: {len(found)} candidates on {' '.join(EXCHANGES)}, {len(todo)} folds to run, "
                  f"{cached} cached, {len(skipped)} skipped, {WORKERS} workers", flush=True)
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
            report.update(vol_target.run(conn))
            report.update(suggest.run(conn))
            report.update(confidence.run(conn))
            report.update(book.run(conn))
            report.update(mixtures.run(conn))
            report.update(scorecard.run(conn))
            report.update(horizon.run(conn))
            ok = failed <= FAIL_SHARE * max(done, 1)
            print(f"walkforward: done: {json.dumps({k: v for k, v in report.items() if k not in ('failures', 'skipped')})}",
                  flush=True)
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s",
                         (Jsonb(report), ok, run_id))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
