#!/usr/bin/env python3
"""Nightly reproduce check: re-run a random sample of cached results and compare them with what is stored.

A cached backtest chunk or walk-forward fold is trusted because its cache key (strategy file, configs, Freqtrade
version, dates, candle digest, hyperopt settings) still matches. That trust holds only if the same inputs give the
same output. This step picks REPRO_CHUNKS chunks and REPRO_FOLDS folds whose stored key equals the key their
inputs give today, runs each again with the real runner, and compares:
- a chunk by its trades (every stored column, sorted by open date and pair) and its final balance;
- a fold by its tuned parameters and the base, tuned and in-sample trade counts and returns.

Anything that differs is a mismatch: the cache would be serving a result the code no longer produces. The run
goes to `runs` as kind 'reproduce' with what was checked and every mismatch; web/exporter.py exports
trader_repro_checked and trader_repro_mismatch, and home-config alerts on a mismatch. The run is ok when every
sampled result could be run again; a mismatch is reported by its own alert, not as a failed run.

It runs after the nightly analysis in the same job (lab/analyze.py's main), one task at a time.
"""

import datetime as dt
import json
import os
import random
import sys

from psycopg.types.json import Jsonb

from lab import analyze, metrics, walkforward
from lab.db import connect
from lab.util import finite

CHUNKS = int(os.environ.get("REPRO_CHUNKS", "3"))
FOLDS = int(os.environ.get("REPRO_FOLDS", "1"))  # a fold reruns its hyperopt: about an hour


def trade_rows(rows) -> list[tuple]:
    """Trades as comparable tuples in TRADE_COLS order, sorted, with timestamps as UTC instants."""
    out = []
    for r in rows:
        r = [finite(v) for v in r]
        out.append(tuple(v.to_pydatetime() if hasattr(v, "to_pydatetime") else v for v in r))
    return sorted(out, key=lambda t: (t[1], t[0], t[2]))


def chunk_sample(conn, rng: random.Random, n: int, ft_version: str) -> list[dict]:
    """Up to n stored, ok chunks whose cache key their inputs still give."""
    rows = conn.execute("""
        SELECT c.id, c.strategy, c.exchange, c.chunk_start, c.chunk_end, c.cache_key, c.final_balance,
               s.file_hash, s.timeframe
        FROM backtest_chunks c JOIN strategies s ON s.name = c.strategy
        WHERE c.ok AND s.load_error IS NULL ORDER BY c.id""").fetchall()
    rng.shuffle(rows)
    digests: dict[tuple[str, str], dict] = {}
    out = []
    for cid, strategy, ex, start, end, key, final, file_hash, tf in rows:
        if len(out) >= n:
            break
        if (ex, tf) not in digests:
            digests[ex, tf] = analyze.candle_digests(ex, tf)
        if analyze.chunk_key(file_hash, ex, start, end, ft_version, digests[ex, tf].get(start.year, "")) == key:
            out.append({"id": cid, "strategy": strategy, "exchange": ex, "start": start, "end": end,
                        "final_balance": final})
    return out


def fold_sample(conn, rng: random.Random, n: int, ft_version: str) -> list[dict]:
    """Up to n stored folds with tuned parameters whose cache key their inputs still give."""
    rows = conn.execute("""
        SELECT w.strategy, w.exchange, w.train_start, w.fold_start, w.test_end, w.cache_key, s.file, s.file_hash,
               s.timeframe, w.params, w.is_trades, w.base_trades, w.base_returns, w.tuned_trades, w.tuned_returns
        FROM walkforward w JOIN strategies s ON s.name = w.strategy
        WHERE w.ok AND w.params IS NOT NULL AND s.load_error IS NULL ORDER BY 1, 2, 4""").fetchall()
    rng.shuffle(rows)
    days: dict[tuple[str, str], dict] = {}
    out = []
    for (strategy, ex, train_start, fold_start, test_end, key, file, file_hash, tf, params, is_trades,
         base_trades, base_returns, tuned_trades, tuned_returns) in rows:
        if len(out) >= n:
            break
        if (ex, tf) not in days:
            days[ex, tf] = analyze.candle_digests(ex, tf, by="day")
        fold = (train_start, fold_start, test_end)
        candles = analyze.span_digest(days[ex, tf], train_start, test_end)
        if walkforward.fold_key(file_hash, ex, fold, ft_version, candles) == key:
            out.append({"strategy": strategy, "exchange": ex, "fold": fold, "file": file,
                        "stored": fold_result(params, is_trades, base_trades, base_returns, tuned_trades, tuned_returns)})
    return out


def fold_result(params, is_trades, base_trades, base_returns, tuned_trades, tuned_returns) -> dict:
    return {"params": params, "is_trades": is_trades, "base_trades": base_trades, "base_returns": list(base_returns or []),
            "tuned_trades": tuned_trades, "tuned_returns": list(tuned_returns or [])}


def check_chunk(conn, runner, c: dict) -> dict:
    res = runner.backtest(c["strategy"], c["exchange"], c["start"], c["end"])
    if not res["ok"]:
        return {"failed": res.get("error") or "failed"}
    stored = trade_rows(conn.execute(f"SELECT {', '.join(analyze.TRADE_COLS)} FROM backtest_trades WHERE chunk_id = %s",
                                     (c["id"],)).fetchall())
    trades = res["trades"]
    again = trade_rows(trades[analyze.TRADE_COLS].itertuples(index=False)) if len(trades) else []
    diff = []
    if stored != again:
        first = next((i for i, (a, b) in enumerate(zip(stored, again)) if a != b), min(len(stored), len(again)))
        diff.append(f"trades: {len(stored)} stored, {len(again)} now, first difference at trade {first}")
    if finite(res["final_balance"]) != c["final_balance"]:
        diff.append(f"final balance: {c['final_balance']} stored, {res['final_balance']} now")
    return {"diff": diff}


def check_fold(runner, f: dict) -> dict:
    res = runner.fold(f["strategy"], f["file"], f["exchange"], f["fold"],
                      metrics.seed_of(f["strategy"], f["exchange"], f["fold"][1]))
    if not res["ok"] or not res.get("params"):
        return {"failed": res.get("error") or "no tuned parameters"}
    side = lambda k: res.get(k) or {}  # noqa: E731
    again = fold_result(res["params"], side("is").get("trades"), side("base").get("trades"), side("base").get("returns"),
                        side("tuned").get("trades"), side("tuned").get("returns"))
    stored = f["stored"]
    return {"diff": [f"{k}: {json.dumps(stored[k])[:120]} stored, {json.dumps(again[k])[:120]} now"
                     for k in stored if finite(again[k]) != stored[k]]}


def main(chunk_runner=None, fold_runner=None, rng: random.Random | None = None) -> int:
    chunk_runner, fold_runner = chunk_runner or analyze.Cli(), fold_runner or walkforward.Cli()
    rng = rng or random.Random()
    started = dt.datetime.now(dt.timezone.utc)
    report: dict = {"revision": analyze.REVISION, "checked": 0, "mismatch": 0, "failed": 0, "results": []}
    ok = False
    with connect(autocommit=True) as conn:
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('reproduce', %s, '{}', %s) "
                              "RETURNING id", (started.strftime("%Y-%m-%d %H:%M:%S"), started)).fetchone()[0]
        try:
            chunks = chunk_sample(conn, rng, CHUNKS, chunk_runner.version())
            folds = fold_sample(conn, rng, FOLDS, fold_runner.version())
            tasks = [("chunk", f"{c['strategy']} {c['exchange']} {c['start']}..{c['end']}", lambda c=c: check_chunk(conn, chunk_runner, c))
                     for c in chunks]
            tasks += [("fold", f"{f['strategy']} {f['exchange']} {f['fold'][1]:%Y-%m}", lambda f=f: check_fold(fold_runner, f))
                      for f in folds]
            print(f"reproduce: {len(chunks)} chunks and {len(folds)} folds sampled", flush=True)
            for what, name, check in tasks:
                try:
                    got = check()
                except Exception as e:  # one task's runner bug must not lose the others
                    got = {"failed": f"{type(e).__name__}: {e}"}
                if "failed" in got:
                    report["failed"] += 1
                else:
                    report["checked"] += 1
                    report["mismatch"] += bool(got["diff"])
                report["results"].append({"kind": what, "name": name, **got})
                print(f"reproduce: {what} {name}: {got.get('failed') or '; '.join(got['diff']) or 'identical'}", flush=True)
            ok = report["failed"] == 0
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s",
                         (Jsonb(report), ok, run_id))
    print(f"reproduce: {report['checked']} checked, {report['mismatch']} mismatched, {report['failed']} failed", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
