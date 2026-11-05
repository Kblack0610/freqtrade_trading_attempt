#!/usr/bin/env python3
"""Weekly walk-forward test: do hyperopt's parameters hold on months it never saw?

Candidates are the league's strategies plus the open promote and watch suggestions, lookahead-clean
and loadable, on WF_EXCHANGE (binanceus: coinbase's 2.4% round trip leaves nothing to tune). For each
of the last WF_FOLDS whole calendar months:
- the month is backtested with the strategy as it is (base);
- hyperopt tunes it on the WF_TRAIN_DAYS before the month, with a seeded random state;
- the month is backtested again with the tuned parameters, and so is the training span (in sample).
A fold is cached by strategy file, configs, Freqtrade version, dates and hyperopt settings, so a weekly
run adds the newest month and whatever a changed file or a new candidate needs.

Hyperopt runs on a copy of the strategy's folder in a temporary userdir, so its params file never lands
next to the real strategy, where every nightly backtest would load it.

Rows go to `walkforward`; lab/suggest.py turns them into retune suggestions and runs at the end.
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

import analyze
import metrics
import suggest
from db import connect

EXCHANGE = os.environ.get("WF_EXCHANGE", "binanceus")
FOLDS = int(os.environ.get("WF_FOLDS", "6"))
TRAIN_DAYS = int(os.environ.get("WF_TRAIN_DAYS", "180"))
EPOCHS = int(os.environ.get("WF_EPOCHS", "150"))
LOSS = os.environ.get("WF_LOSS", "SharpeHyperOptLossDaily")
MIN_TRADES = int(os.environ.get("WF_MIN_TRADES", "10"))  # hyperopt ignores an epoch with fewer training trades
LIMIT = int(os.environ.get("WF_LIMIT", "16"))
WORKERS = int(os.environ.get("WF_WORKERS", "6"))
TIMEOUT = int(os.environ.get("WF_TIMEOUT", "10800"))
ONLY = set(os.environ.get("WF_STRATEGIES", "").split())
MAX_ATTEMPTS = analyze.MAX_ATTEMPTS
FAIL_SHARE = analyze.FAIL_SHARE


def folds(today: dt.date, n: int | None = None, train_days: int | None = None) -> list[tuple[dt.date, dt.date, dt.date]]:
    """(train_start, test_start, test_end) for the last n (FOLDS) whole calendar months, oldest first."""
    n, train_days = n or FOLDS, train_days or TRAIN_DAYS
    out, end = [], today.replace(day=1)
    for _ in range(n):
        start = (end - dt.timedelta(days=1)).replace(day=1)
        out.append((start - dt.timedelta(days=train_days), start, end))
        end = start
    return out[::-1]


def fold_key(file_hash: str, exchange: str, fold: tuple, ft_version: str) -> str:
    cfg = "".join(analyze.sha256(p.read_bytes()) for p in analyze.config_files(exchange))
    return analyze.sha256(json.dumps([file_hash, cfg, ft_version, exchange, [str(d) for d in fold],
                                      EPOCHS, LOSS, MIN_TRADES]).encode())


class Runner(Protocol):
    def version(self) -> str: ...
    def fold(self, strategy: str, file: str, exchange: str, fold: tuple, seed: int) -> dict: ...


def stats(res: dict) -> dict:
    """A backtest result as the trades, total return and per-trade profit ratios a fold row keeps."""
    t = res["trades"]
    ret = [float(x) for x in t["profit_ratio"]] if len(t) else []
    return {"trades": len(ret), "profit": res["final_balance"] / res["start_balance"] - 1.0,
            "expectancy": sum(ret) / len(ret) if ret else None, "returns": ret}


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
            base = self.bt.backtest(strategy, exchange, test_start, test_end, strat)
            if not base["ok"]:
                return took({"ok": False, "no_data": base.get("no_data", False), "error": f"base: {base['error']}"})
            before = params_file.stat().st_mtime_ns if params_file.exists() else None
            cmd = ["freqtrade", "hyperopt", "--userdir", d,
                   *[a for p in analyze.config_files(exchange) for a in ("-c", str(p))],
                   "--datadir", str(analyze.RESULTS / "data" / exchange), "--strategy", strategy,
                   "--strategy-path", str(strat), "--timerange", f"{train_start:%Y%m%d}-{test_start:%Y%m%d}",
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
            for side, (a, b) in (("tuned", (test_start, test_end)), ("is", (train_start, test_start))):
                r = self.bt.backtest(strategy, exchange, a, b, strat)
                if not r["ok"]:
                    return took({"ok": False, "error": f"{side}: {r['error']}"})
                out[side] = stats(r)
        return took(out)


def candidates(conn) -> list[dict]:
    """League strategies first, then promote and watch suggestions by score; loadable and lookahead-clean."""
    rows = conn.execute("""
        SELECT s.name, s.file, s.file_hash,
               min(CASE WHEN b.name IS NOT NULL THEN 0 WHEN g.kind = 'promote' THEN 1 ELSE 2 END),
               max(g.score)
        FROM strategies s
        LEFT JOIN bots b ON b.strategy = s.name AND b.last_seen >= now() - interval '1 day'
        LEFT JOIN suggestions g ON g.strategy = s.name AND g.kind IN ('promote', 'watch')
        WHERE s.load_error IS NULL AND (b.name IS NOT NULL OR g.kind IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM bias_checks c WHERE c.strategy = s.name AND c.kind = 'lookahead' AND c.has_bias)
        GROUP BY 1, 2, 3 ORDER BY 4, 5 DESC NULLS LAST, 1""").fetchall()
    out = [{"name": n, "file": f, "file_hash": h} for n, f, h, _, _ in rows if not ONLY or n in ONLY]
    return out[:LIMIT]


def plan(conn, found: list[dict], today: dt.date, ft_version: str) -> tuple[list[dict], int]:
    done = {(s, e, f): (k, ok, n) for s, e, f, k, ok, n in conn.execute(
        "SELECT strategy, exchange, fold_start, cache_key, ok, attempts FROM walkforward")}
    todo, cached = [], 0
    for s in found:
        for fold in folds(today):
            key = fold_key(s["file_hash"], EXCHANGE, fold, ft_version)
            prior = done.get((s["name"], EXCHANGE, fold[1]))
            if prior and prior[0] == key and (prior[1] or prior[2] >= MAX_ATTEMPTS):
                cached += 1
                continue
            todo.append({**s, "fold": fold, "key": key,
                         "attempts": prior[2] if prior and prior[0] == key and not prior[1] else 0})
    todo.sort(key=lambda t: (-t["fold"][1].toordinal(), t["name"]))  # the newest month first
    return todo, cached


def store(conn, task: dict, res: dict) -> None:
    side = lambda k: res.get(k) or {}  # noqa: E731
    attempts = MAX_ATTEMPTS if res.get("no_data") else task["attempts"] + 1 if not res["ok"] else 1
    train_start, fold_start, test_end = task["fold"]
    conn.execute("""
        INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, attempts, error,
            epochs, loss, params, is_trades, is_profit, base_trades, base_profit, base_expectancy, base_returns,
            tuned_trades, tuned_profit, tuned_expectancy, tuned_returns, duration_s, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (strategy, exchange, fold_start) DO UPDATE SET train_start = EXCLUDED.train_start,
            test_end = EXCLUDED.test_end, cache_key = EXCLUDED.cache_key, ok = EXCLUDED.ok, attempts = EXCLUDED.attempts,
            error = EXCLUDED.error, epochs = EXCLUDED.epochs, loss = EXCLUDED.loss, params = EXCLUDED.params,
            is_trades = EXCLUDED.is_trades, is_profit = EXCLUDED.is_profit, base_trades = EXCLUDED.base_trades,
            base_profit = EXCLUDED.base_profit, base_expectancy = EXCLUDED.base_expectancy,
            base_returns = EXCLUDED.base_returns, tuned_trades = EXCLUDED.tuned_trades,
            tuned_profit = EXCLUDED.tuned_profit, tuned_expectancy = EXCLUDED.tuned_expectancy,
            tuned_returns = EXCLUDED.tuned_returns, duration_s = EXCLUDED.duration_s, computed_at = now()""",
        (task["name"], EXCHANGE, fold_start, train_start, test_end, task["key"], res["ok"], attempts, res.get("error"),
         EPOCHS, LOSS, Jsonb(res["params"]) if res.get("params") else None,
         side("is").get("trades"), side("is").get("profit"),
         side("base").get("trades"), side("base").get("profit"), side("base").get("expectancy"), side("base").get("returns"),
         side("tuned").get("trades"), side("tuned").get("profit"), side("tuned").get("expectancy"),
         side("tuned").get("returns"), res.get("duration_s")))


def main(runner: Runner | None = None, today: dt.date | None = None) -> int:
    runner = runner or Cli()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    with connect(autocommit=True) as conn:
        started = dt.datetime.now(dt.timezone.utc)
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('walkforward', %s, '{}', %s) "
                              "RETURNING id", (started.strftime("%Y-%m-%d %H:%M:%S"), started)).fetchone()[0]
        report: dict = {"exchange": EXCHANGE, "folds": FOLDS, "train_days": TRAIN_DAYS, "epochs": EPOCHS, "loss": LOSS}
        ok = False
        try:
            found = candidates(conn)
            todo, cached = plan(conn, found, today, runner.version())
            report.update(candidates=[s["name"] for s in found], folds_run=len(todo), folds_cached=cached)
            print(f"walkforward: {len(found)} candidates, {len(todo)} folds to run, {cached} cached, "
                  f"{WORKERS} workers", flush=True)
            failed, done, failures = 0, 0, []
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = {pool.submit(runner.fold, t["name"], t["file"], EXCHANGE, t["fold"],
                                    metrics.seed_of(t["name"], EXCHANGE, t["fold"][1])): t for t in todo}
                try:
                    for fut in as_completed(futs):
                        task = futs[fut]
                        try:
                            res = fut.result()
                        except Exception as e:  # one fold's runner bug must not lose the others
                            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        store(conn, task, res)
                        done += 1
                        if not res["ok"] and not res.get("no_data"):
                            failed += 1
                            failures.append(f"{task['name']} {task['fold'][1]:%Y-%m}: {res['error']}"[:300])
                        print(f"walkforward: {done}/{len(todo)} folds, {failed} failed", flush=True)
                except BaseException:
                    pool.shutdown(cancel_futures=True)
                    raise
            report.update(folds_failed=failed, failures=failures[:100])
            report.update(suggest.run(conn))
            ok = failed <= FAIL_SHARE * max(len(todo), 1)
            print(f"walkforward: done: {json.dumps({k: v for k, v in report.items() if k != 'failures'})}", flush=True)
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s",
                         (Jsonb(report), ok, run_id))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
