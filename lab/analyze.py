#!/usr/bin/env python3
"""Nightly analysis: every strategy on every exchange's full price history, and what follows from it.

1. Strategies: every file the resolver finds under user_data/strategies, with its hash, into
   `strategies`.
2. Backtests: each strategy x exchange over its whole history, one calendar year per chunk, run in
   parallel (ANALYZE_WORKERS freqtrade processes). A chunk is cached by its cache_key (strategy file,
   config files, Freqtrade version, range): past years run once, the current year reruns each night.
   Its trades go to `backtest_trades`. A chunk starts from the config's wallet, so a long history is
   many short compounding runs chained by their daily returns, not one run that compounds for years.
3. Bias: lookahead-analysis and recursive-analysis per strategy, rerun when its file hash changes.
4. Derived, rebuilt in one transaction from the stored trades: daily returns, metrics over 30d, 90d,
   180d, 365d and all (with bootstrap intervals and the deflated Sharpe across every strategy x
   exchange), Monte Carlo estimates, BTC regimes, and correlation with the strategies the league runs.
5. Drift: each league bot's strategy backtested on its exchange over the time the bot has run, set
   against the bot's dry-run trades in `drift`.
6. Suggestions: lab/suggest.py turns those tables into promote, watch, demote, retire and fix rows,
   with the thresholds in lab/suggest.toml.

Returns are realized (on trade close); a trade still open at a chunk's end is closed there.
A year with no candles in the data (binanceus has none from 2023-07 to 2025-02, when its USD pairs were
suspended) is stored as empty, never retried, and is not a failure.
Exits 1 when more than a tenth of the chunks run this time failed, or on a crash.

`python lab/analyze.py bias-one <lookahead|recursive> <strategy> [--strategy-path DIR]` runs one bias
check and prints one BIAS_RESULT line; the main run calls it as a subprocess per check.
"""

import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

import metrics
import suggest
from db import connect

REPO = Path(os.environ.get("REPO", "/repo"))
RESULTS = Path(os.environ.get("LAB_RESULTS", "/lab"))
USER_DATA = REPO / "user_data"
EXCHANGES = os.environ.get("ANALYZE_EXCHANGES", "binanceus coinbase").split()
ONLY = set(os.environ.get("ANALYZE_STRATEGIES", "").split())
WORKERS = int(os.environ.get("ANALYZE_WORKERS", "6"))
BIAS_EXCHANGE = os.environ.get("BIAS_EXCHANGE", "binanceus")
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "120"))
RECURSIVE_DAYS = int(os.environ.get("RECURSIVE_DAYS", "30"))
RECURSIVE_TOL = float(os.environ.get("RECURSIVE_TOL", "0.001"))  # relative indicator change still called converged
REGIME_EXCHANGE = os.environ.get("REGIME_EXCHANGE", "coinbase")
CHUNK_TIMEOUT = int(os.environ.get("CHUNK_TIMEOUT", "3600"))
MAX_ATTEMPTS = 3
WINDOWS = {"30d": 30, "90d": 90, "180d": 180, "365d": 365, "all": None}
HORIZONS = (30, 90)
ESTIMATE_BASIS, ESTIMATE_MIN_DAYS = 365, 180
CORR_DAYS, CORR_MIN_DAYS = 365, 60
FAIL_SHARE = 0.10
NO_DATA = "no candles in this period"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_files(exchange: str) -> list[Path]:
    return [USER_DATA / "config.base.json", USER_DATA / "exchanges" / f"{exchange}.json"]


def wallet() -> float:
    return float(json.loads((USER_DATA / "config.base.json").read_text()).get("dry_run_wallet", 1000))


def chunk_key(file_hash: str, exchange: str, start: dt.date, end: dt.date, ft_version: str) -> str:
    cfg = "".join(sha256(p.read_bytes()) for p in config_files(exchange))
    return sha256(json.dumps([file_hash, cfg, ft_version, exchange, str(start), str(end)]).encode())


def chunks(first: dt.date, today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Calendar years from the first candle's year up to today; the current year ends today."""
    out = []
    for y in range(first.year, today.year + 1):
        start, end = dt.date(y, 1, 1), min(dt.date(y + 1, 1, 1), today)
        if end > start:
            out.append((start, end))
    return out


class Runner(Protocol):
    def version(self) -> str: ...
    def discover(self) -> list[dict]: ...
    def backtest(self, strategy: str, exchange: str, start: dt.date, end: dt.date) -> dict: ...
    def bias(self, kind: str, strategy: str) -> dict: ...


def _errors(text: str) -> str:
    lines = text.splitlines()
    bad = [ln.split(" - ERROR - ", 1)[-1] for ln in lines if " - ERROR - " in ln or "Error" in ln]
    return " | ".join(bad[-3:] or lines[-3:])[:800]


def load_error(source: str, name: str) -> str | None:
    """freqtrade loads a strategy by name only from a file containing the literal `class <name>(`, so a space before the paren imports fine yet never loads."""
    return None if f"class {name}(" in source else f"freqtrade cannot load it by name: write `class {name}(` with no space"


class Cli:
    """The real thing: freqtrade subprocesses, so one strategy's crash or leak stays in its process."""

    def version(self) -> str:
        import freqtrade
        return freqtrade.__version__

    def discover(self) -> list[dict]:
        from freqtrade.configuration import Configuration
        from freqtrade.resolvers import StrategyResolver

        cfg = Configuration.from_files([str(p) for p in config_files(BIAS_EXCHANGE)])
        cfg["user_data_dir"] = USER_DATA
        cfg["recursive_strategy_search"] = True
        out = []
        for o in StrategyResolver.search_all_objects(cfg, enum_failed=True, recursive=True):
            loc = Path(o["location"])
            cls = o.get("class")
            name = o.get("name") or loc.stem
            out.append({"name": name, "file": str(loc.relative_to(REPO)),
                        "file_hash": sha256(loc.read_bytes()),
                        "timeframe": getattr(cls, "timeframe", None) if cls else None,
                        "startup_candle_count": getattr(cls, "startup_candle_count", None) if cls else None,
                        "load_error": load_error(loc.read_text(encoding="utf-8"), name) if cls else "failed to import"})
        return out

    def backtest(self, strategy: str, exchange: str, start: dt.date, end: dt.date,
                 strategy_path: Path | None = None) -> dict:
        """strategy_path puts a folder ahead of the repo's, so its copy of the strategy (and params file) loads."""
        from freqtrade.data.btanalysis import load_backtest_data, load_backtest_stats

        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="bt-") as d:
            cmd = ["freqtrade", "backtesting", "--userdir", str(USER_DATA), "--recursive-strategy-search",
                   *[a for p in config_files(exchange) for a in ("-c", str(p))],
                   "--datadir", str(RESULTS / "data" / exchange), "--strategy", strategy,
                   "--timerange", f"{start:%Y%m%d}-{end:%Y%m%d}", "--export", "trades",
                   "--backtest-directory", d, "--cache", "none"]
            if strategy_path:
                cmd += ["--strategy-path", str(strategy_path)]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=CHUNK_TIMEOUT)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"timed out after {CHUNK_TIMEOUT}s", "duration_s": time.monotonic() - t0}
            if p.returncode and "No data found" in p.stdout + p.stderr:
                return {"ok": False, "no_data": True, "error": NO_DATA, "duration_s": time.monotonic() - t0}
            # freqtrade exits 0 on some errors; the result zip is the proof of a run
            if p.returncode or not list(Path(d).glob("*.zip")):
                return {"ok": False, "error": f"exit {p.returncode}: {_errors(p.stdout + p.stderr)}",
                        "duration_s": time.monotonic() - t0}
            s = load_backtest_stats(Path(d))["strategy"][strategy]
            trades = load_backtest_data(Path(d), strategy)
        return {"ok": True, "error": None, "duration_s": time.monotonic() - t0, "trades": trades,
                "bt_start": metrics.utc(s["backtest_start"]), "bt_end": metrics.utc(s["backtest_end"]),
                "start_balance": float(s["starting_balance"]), "final_balance": float(s["final_balance"]),
                "max_drawdown": float(s.get("max_drawdown_account") or 0.0)}

    def bias(self, kind: str, strategy: str) -> dict:
        cmd = [sys.executable, str(Path(__file__).resolve()), "bias-one", kind, strategy]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=CHUNK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"has_bias": None, "error": f"timed out after {CHUNK_TIMEOUT}s"}
        for line in reversed(p.stdout.splitlines()):
            if line.startswith("BIAS_RESULT "):
                return json.loads(line.removeprefix("BIAS_RESULT "))
        return {"has_bias": None, "error": f"exit {p.returncode}: {_errors(p.stdout + p.stderr)}"}


def bias_one(kind: str, strategy: str, strategy_path: str | None) -> dict:
    """One check in this process. Lookahead: does a signal change when later candles are hidden.
    Recursive: does an indicator still move at the strategy's own startup_candle_count."""
    from freqtrade.commands import Arguments
    from freqtrade.configuration import setup_utils_configuration
    from freqtrade.enums import RunMode
    from freqtrade.optimize.analysis.lookahead_helpers import LookaheadAnalysisSubFunctions as Look
    from freqtrade.optimize.analysis.recursive_helpers import RecursiveAnalysisSubFunctions as Rec
    from freqtrade.resolvers import StrategyResolver
    from freqtrade.util.progress_tracker import get_progress_tracker

    days = LOOKAHEAD_DAYS if kind == "lookahead" else RECURSIVE_DAYS
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y%m%d")
    argv = [f"{kind}-analysis", "--userdir", str(USER_DATA), "--recursive-strategy-search", *[a for p in config_files(BIAS_EXCHANGE) for a in ("-c", str(p))],
            "--datadir", str(RESULTS / "data" / BIAS_EXCHANGE), "--strategy", strategy, "--timerange", f"{since}-"]
    if strategy_path:
        argv += ["--strategy-path", strategy_path]
    try:
        cfg = setup_utils_configuration(Arguments(argv).get_parsed_arg(), RunMode.UTIL_NO_EXCHANGE)
        helper = Look if kind == "lookahead" else Rec
        cfg = helper.calculate_config_overrides(cfg)
        objs = [o for o in StrategyResolver.search_all_objects(cfg, enum_failed=False, recursive=True)
                if o["name"] == strategy]
        if not objs:
            return {"has_bias": None, "error": "strategy not found"}
        with get_progress_tracker() as progress:
            if kind == "lookahead":
                inst = helper.initialize_single_lookahead_analysis(cfg, objs[0], progress)
            else:
                inst = helper.initialize_single_recursive_analysis(cfg, objs[0], progress)
    except Exception as e:
        return {"has_bias": None, "error": f"{type(e).__name__}: {e}"[:800]}
    if kind == "lookahead":
        a = inst.current_analysis
        detail = {"signals": a.total_signals, "false_entries": a.false_entry_signals, "false_exits": a.false_exit_signals}
        if inst.failed_bias_check:
            return {"has_bias": None, "detail": detail, "error": f"only {a.total_signals} signals in {days} days"}
        return {"has_bias": bool(a.has_bias), "biased_indicators": ", ".join(a.false_indicators) or None, "detail": detail}
    scc = inst._strat_scc
    at_own = {ind: d.get(scc) for ind, d in inst.dict_recursive.items()}
    biased = sorted(ind for ind, v in at_own.items() if v is not None and math.isfinite(v) and abs(v) > RECURSIVE_TOL)
    detail = {"startup_candle_count": scc, "tolerance": RECURSIVE_TOL,
              "diff": {ind: {str(k): v for k, v in d.items()} for ind, d in inst.dict_recursive.items()}}
    return {"has_bias": bool(biased), "biased_indicators": ", ".join(biased) or None, "detail": detail}


TRADE_COLS = ["pair", "open_date", "close_date", "open_rate", "close_rate", "stake_amount", "profit_ratio",
              "profit_abs", "exit_reason", "enter_tag"]


def store_chunk(conn, task: dict, res: dict, run_id: int) -> None:
    attempts = MAX_ATTEMPTS if res.get("no_data") else task["attempts"] + 1 if not res["ok"] else 1
    with conn.transaction():
        conn.execute("DELETE FROM backtest_chunks WHERE strategy = %s AND exchange = %s AND chunk_start = %s",
                     (task["strategy"], task["exchange"], task["start"]))
        trades = res.get("trades")
        chunk_id = conn.execute(
            "INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok, attempts, error,"
            " bt_start, bt_end, trades, start_balance, final_balance, max_drawdown, duration_s, run_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (task["strategy"], task["exchange"], task["start"], task["end"], task["key"], res["ok"], attempts,
             res.get("error"), res.get("bt_start"), res.get("bt_end"), None if trades is None else len(trades),
             res.get("start_balance"), res.get("final_balance"), res.get("max_drawdown"), res.get("duration_s"),
             run_id)).fetchone()[0]
        if trades is not None and len(trades):
            with conn.cursor().copy(f"COPY backtest_trades (chunk_id, {', '.join(TRADE_COLS)}) FROM STDIN") as cp:
                for row in trades[TRADE_COLS].itertuples(index=False):
                    cp.write_row([chunk_id, *[None if isinstance(v, float) and math.isnan(v) else v for v in row]])


def finite(v):
    """jsonb has no NaN or Infinity, which Python's json writes, so a non-finite number is stored as null."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [finite(x) for x in v]
    return v


def store_bias(conn, strategy: str, kind: str, file_hash: str, res: dict) -> None:
    conn.execute(
        "INSERT INTO bias_checks (strategy, kind, file_hash, has_bias, biased_indicators, detail, error, checked_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, now()) ON CONFLICT (strategy, kind) DO UPDATE SET"
        " file_hash = EXCLUDED.file_hash, has_bias = EXCLUDED.has_bias, biased_indicators = EXCLUDED.biased_indicators,"
        " detail = EXCLUDED.detail, error = EXCLUDED.error, checked_at = now()",
        (strategy, kind, file_hash, res.get("has_bias"), res.get("biased_indicators"),
         Jsonb(finite(res["detail"])) if res.get("detail") is not None else None, res.get("error")))


def sync_strategies(conn, found: list[dict]) -> None:
    with conn.transaction():
        for s in found:
            conn.execute(
                "INSERT INTO strategies (name, file, file_hash, timeframe, startup_candle_count, load_error, seen_at)"
                " VALUES (%(name)s, %(file)s, %(file_hash)s, %(timeframe)s, %(startup_candle_count)s, %(load_error)s, now())"
                " ON CONFLICT (name) DO UPDATE SET file = EXCLUDED.file, file_hash = EXCLUDED.file_hash,"
                " timeframe = EXCLUDED.timeframe, startup_candle_count = EXCLUDED.startup_candle_count,"
                " load_error = EXCLUDED.load_error, seen_at = now()", s)
        names = [s["name"] for s in found]
        # a deleted strategy takes its results with it
        for table in ("strategies", "bias_checks"):
            col = "name" if table == "strategies" else "strategy"
            conn.execute(f"DELETE FROM {table} WHERE NOT ({col} = ANY(%s))", (names,))
        conn.execute("DELETE FROM backtest_chunks WHERE NOT (strategy = ANY(%s)) OR NOT (exchange = ANY(%s))",
                     (names, EXCHANGES))


def coverage(conn) -> dict[tuple[str, str], dt.datetime]:
    """The first candle per exchange x timeframe."""
    return {(ex, tf): f for ex, tf, f in conn.execute(
        "SELECT exchange, timeframe, min(first_ts) FROM data_coverage WHERE first_ts IS NOT NULL GROUP BY 1, 2")}


def plan(conn, found: list[dict], first: dict, today: dt.date, ft_version: str) -> tuple[list[dict], int, list[str]]:
    """The chunks to run this time, how many are cached, and why some strategy x exchange is skipped."""
    done = {(s, e, c): (k, ok, n) for s, e, c, k, ok, n in conn.execute(
        "SELECT strategy, exchange, chunk_start, cache_key, ok, attempts FROM backtest_chunks")}
    todo, cached, skipped = [], 0, []
    for s in found:
        if s["load_error"] or (ONLY and s["name"] not in ONLY):
            continue
        for ex in EXCHANGES:
            since = first.get((ex, s["timeframe"]))
            if since is None:
                skipped.append(f"{s['name']} on {ex}: no {s['timeframe']} candles")
                continue
            for start, end in chunks(since.date(), today):
                key = chunk_key(s["file_hash"], ex, start, end, ft_version)
                prior = done.get((s["name"], ex, start))
                if prior and prior[0] == key and (prior[1] or prior[2] >= MAX_ATTEMPTS):
                    cached += 1
                    continue
                attempts = prior[2] if prior and prior[0] == key and not prior[1] else 0
                todo.append({"strategy": s["name"], "exchange": ex, "start": start, "end": end, "key": key,
                             "attempts": attempts})
    # the newest years first: they feed every window, so a run cut short still updates the page
    todo.sort(key=lambda t: (-t["start"].year, t["strategy"], t["exchange"]))
    return todo, cached, skipped


def bias_todo(conn, found: list[dict], first: dict) -> list[tuple[str, str, str]]:
    have = {(s, k): (h, err, at) for s, k, h, err, at in conn.execute(
        "SELECT strategy, kind, file_hash, error, checked_at FROM bias_checks")}
    week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    out = []
    for s in found:
        if s["load_error"] or (ONLY and s["name"] not in ONLY) or (BIAS_EXCHANGE, s["timeframe"]) not in first:
            continue
        for kind in ("lookahead", "recursive"):
            prior = have.get((s["name"], kind))
            if prior and prior[0] == s["file_hash"] and (prior[1] is None or prior[2] > week_ago):
                continue
            out.append((s["name"], kind, s["file_hash"]))
    return out


def drift_todo(conn, found: list[dict], today: dt.date) -> list[dict]:
    """League bots with a live period to compare: from the bot's first sighting or first trade, whichever is earlier."""
    ok = {s["name"]: s for s in found if not s["load_error"]}
    out = []
    for bot, ex, strategy, since in conn.execute(
            "SELECT b.name, b.exchange, b.strategy, least(b.first_seen, min(t.open_date)) FROM bots b"
            " LEFT JOIN bot_trades t ON t.bot = b.name GROUP BY b.name ORDER BY b.name"):
        if strategy in ok and (not ONLY or strategy in ONLY) and since.date() < today:
            out.append({"bot": bot, "exchange": ex, "strategy": strategy, "since": since,
                        "timeframe": ok[strategy]["timeframe"], "start": since.date(), "end": today + dt.timedelta(days=1)})
    return out


DRIFT_COLS = ["bot", "strategy", "exchange", "since", "bt_start", "bt_end", "live_entries", "bt_entries", "matched",
              "live_closed", "bt_closed", "live_expectancy", "bt_expectancy", "live_profit", "bt_profit", "gap",
              "gap_trades", "error"]


def store_drift(conn, task: dict, res: dict) -> None:
    row = {k: task[k] for k in ("bot", "strategy", "exchange", "since")}
    if res.get("ok"):
        lo, hi = max(task["since"], res["bt_start"]), res["bt_end"]
        live = pd.DataFrame(conn.execute(
            "SELECT pair, open_date, is_open, profit_ratio, profit_abs FROM bot_trades"
            " WHERE bot = %s AND open_date >= %s AND open_date < %s", (task["bot"], lo, hi)).fetchall(),
            columns=["pair", "open_date", "is_open", "profit_ratio", "profit_abs"])
        bt = res["trades"]
        if len(bt):
            opened = pd.to_datetime(bt["open_date"], utc=True)
            bt = bt[(opened >= lo) & (opened < hi)]
        row.update(bt_start=res["bt_start"], bt_end=res["bt_end"],
                   **metrics.drift(live, bt, pd.Timedelta(task["timeframe"] or "5m")))
    else:
        row["error"] = res.get("error")
    cols = ", ".join(DRIFT_COLS)
    conn.execute(f"INSERT INTO drift ({cols}) VALUES ({', '.join(['%s'] * len(DRIFT_COLS))}) ON CONFLICT (bot) DO UPDATE"
                 " SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in DRIFT_COLS[1:]) + ", computed_at = now()",
                 [clean(row.get(c)) for c in DRIFT_COLS])


def load_series(conn) -> dict[tuple[str, str], tuple[pd.DataFrame, pd.DataFrame]]:
    """Daily returns and closed trades of every strategy x exchange, from its successful chunks."""
    chunk_rows = conn.execute(
        "SELECT c.id, c.strategy, c.exchange, c.bt_start, c.bt_end, c.start_balance FROM backtest_chunks c"
        " JOIN strategies s ON s.name = c.strategy AND s.load_error IS NULL"
        " WHERE c.ok AND c.exchange = ANY(%s) ORDER BY c.strategy, c.exchange, c.chunk_start", (EXCHANGES,)).fetchall()
    trades = pd.DataFrame(conn.execute(
        "SELECT chunk_id, open_date, close_date, open_rate, close_rate, stake_amount, profit_ratio, profit_abs"
        " FROM backtest_trades"
    ).fetchall(), columns=["chunk_id", "open_date", "close_date", "open_rate", "close_rate", "stake_amount", "profit_ratio",
                           "profit_abs"])
    by_chunk = dict(tuple(trades.groupby("chunk_id"))) if len(trades) else {}
    empty = trades.iloc[0:0]
    parts: dict[tuple[str, str], list] = {}
    for cid, s, e, t0, t1, bal in chunk_rows:
        t = by_chunk.get(cid, empty)
        t = t.assign(share=t["stake_amount"].astype(float) / (bal or wallet()))
        parts.setdefault((s, e), []).append((metrics.daily_returns(t, t0, t1, bal or wallet()), t))
    out = {}
    for k, ps in parts.items():
        daily = pd.concat([d for d, _ in ps], ignore_index=True).drop_duplicates("day", keep="last")
        out[k] = (daily.sort_values("day").reset_index(drop=True), pd.concat([t for _, t in ps], ignore_index=True))
    return out


def window_rows(series: dict) -> list[dict]:
    rows = []
    for (s, e), (daily, trades) in series.items():
        if daily.empty:
            continue
        end = daily["day"].iloc[-1]
        for win, n in WINDOWS.items():
            if n is not None and daily["day"].iloc[0] > end - dt.timedelta(days=n - 1):
                continue  # not that much history
            d = daily if n is None else daily[daily["day"] > end - dt.timedelta(days=n)]
            lo = pd.Timestamp(d["day"].iloc[0], tz="UTC")
            t = trades[pd.to_datetime(trades["close_date"], utc=True) >= lo]
            m = metrics.window_metrics(d.reset_index(drop=True), t, metrics.seed_of(s, e, win))
            rows.append({"strategy": s, "exchange": e, "win": win, **m})
    for win in WINDOWS:
        trial = [r for r in rows if r["win"] == win]
        for r in trial:
            r["dsr"] = metrics.deflated_sharpe(r["_sr_daily"], r["days"], r["_skew"], r["_kurt"], len(trial))
            r["trials"] = len(trial)
    return rows


def hold_columns(rows: list[dict], series: dict, close: pd.Series) -> None:
    """Add each window's share of capital in the market and its return over holding that share of BTC."""
    for r in rows:
        daily, trades = series[(r["strategy"], r["exchange"])]
        start = pd.Timestamp(r["start_day"], tz="UTC")
        end = pd.Timestamp(r["end_day"], tz="UTC") + pd.Timedelta(days=1)
        t = trades[pd.to_datetime(trades["close_date"], utc=True) >= start]
        capital = metrics.capital_exposure(t, start, end)
        excess = metrics.hold_excess(daily[daily["day"] >= r["start_day"]], close, capital,
                                     metrics.seed_of(r["strategy"], r["exchange"], r["win"], "hold"))
        r.update(zip(("hold_excess", "hold_excess_lo", "hold_excess_hi"), excess), capital_exposure=capital)


def estimate_rows(series: dict) -> list[dict]:
    rows = []
    for (s, e), (daily, _) in series.items():
        basis = daily["ret"].to_numpy()[-ESTIMATE_BASIS:]
        if len(basis) < ESTIMATE_MIN_DAYS:
            continue
        for h in HORIZONS:
            seed = metrics.seed_of(s, e, h)
            rows.append({"strategy": s, "exchange": e, "horizon_days": h, "seed": seed, "basis_days": len(basis),
                         **metrics.monte_carlo(basis, h, seed)})
    return rows


def correlation_rows(series: dict, league: set[str]) -> list[dict]:
    rows = []
    for (s, e), (daily, _) in series.items():
        mine = daily.set_index("day")["ret"].iloc[-CORR_DAYS:]
        for other in sorted(league - {s}):
            if (other, e) not in series:
                continue
            theirs = series[(other, e)][0].set_index("day")["ret"]
            both = pd.concat([mine, theirs], axis=1, join="inner").dropna()
            if len(both) < CORR_MIN_DAYS or (both.std() == 0).any():
                continue
            rows.append({"strategy": s, "exchange": e, "other": other, "rho": float(both.iloc[:, 0].corr(both.iloc[:, 1])),
                         "days": len(both)})
    return rows


def btc_close() -> pd.Series:
    path = RESULTS / "data" / REGIME_EXCHANGE / "BTC_USD-1d.feather"
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_feather(path, columns=["date", "close"])
    return pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["date"], utc=True).dt.date)


def regime_rows(close: pd.Series) -> list[dict]:
    if close.empty:
        return []
    r = metrics.regimes(close)
    return [{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
            for row in r.to_dict("records")]


def clean(v):
    if isinstance(v, (np.floating, float)):
        return None if not math.isfinite(v) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    return v


def insert_many(conn, table: str, rows: list[dict], cols: list[str]) -> None:
    if not rows:
        return
    with conn.cursor().copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row([clean(r.get(c)) for c in cols])


WINDOW_COLS = ["strategy", "exchange", "win", "days", "start_day", "end_day", "trades", "total_return", "cagr", "sharpe",
               "sharpe_lo", "sharpe_hi", "sortino", "calmar", "max_drawdown", "profit_factor", "expectancy",
               "expectancy_lo", "expectancy_hi", "gross_expectancy", "gross_expectancy_lo", "gross_expectancy_hi",
               "fee_per_trade", "winrate", "exposure", "capital_exposure", "hold_excess", "hold_excess_lo", "hold_excess_hi",
               "months_profitable", "dsr", "trials"]
ESTIMATE_COLS = ["strategy", "exchange", "horizon_days", "p10", "p50", "p90", "prob_loss", "exp_max_drawdown", "paths",
                 "seed", "basis_days"]


def derive(conn) -> dict:
    series = load_series(conn)
    league = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM bots")}
    daily = [{"strategy": s, "exchange": e, **r} for (s, e), (d, _) in series.items() for r in d.to_dict("records")]
    windows, estimates = window_rows(series), estimate_rows(series)
    close = btc_close()
    hold_columns(windows, series, close)
    corr, reg = correlation_rows(series, league), regime_rows(close)
    with conn.transaction():
        conn.execute("TRUNCATE strategy_daily, strategy_windows, estimates, correlations, regimes")
        insert_many(conn, "strategy_daily", daily, ["strategy", "exchange", "day", "ret", "pnl", "trades"])
        insert_many(conn, "strategy_windows", windows, WINDOW_COLS)
        insert_many(conn, "estimates", estimates, ESTIMATE_COLS)
        insert_many(conn, "correlations", corr, ["strategy", "exchange", "other", "rho", "days"])
        insert_many(conn, "regimes", reg, ["day", "btc_close", "trend", "vol_30d", "vol_bucket"])
    return {"series": len(series), "daily_rows": len(daily), "windows": len(windows), "estimates": len(estimates),
            "correlations": len(corr), "regime_days": len(reg)}


def main(runner: Runner | None = None, today: dt.date | None = None) -> int:
    runner = runner or Cli()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    started = dt.datetime.now(dt.timezone.utc)
    summary: dict = {"exchanges": EXCHANGES, "only": sorted(ONLY)}
    with connect(autocommit=True) as conn:
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('analysis', %s, '{}', %s) RETURNING id",
                              (started.strftime("%Y-%m-%d %H:%M:%S"), started)).fetchone()[0]
        ok = False
        try:
            found = runner.discover()
            sync_strategies(conn, found)
            version = runner.version()
            first = coverage(conn)
            todo, cached, skipped = plan(conn, found, first, today, version)
            bias = bias_todo(conn, found, first)
            drifts = drift_todo(conn, found, today)
            hashes = {s["name"]: s["file_hash"] for s in found}
            summary.update(freqtrade=version, strategies=len(found), load_failed=sum(bool(s["load_error"]) for s in found),
                           chunks_cached=cached, chunks_run=len(todo), skipped=skipped, bias_run=len(bias),
                           drift_run=len(drifts))
            print(f"analyze: {len(found)} strategies, {len(todo)} chunks to run, {cached} cached, "
                  f"{len(bias)} bias checks, {WORKERS} workers", flush=True)
            failed, empty, done, failures = 0, 0, 0, []
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = {pool.submit(runner.bias, kind, s): ("bias", (s, kind, h)) for s, kind, h in bias}
                futs.update({pool.submit(runner.backtest, t["strategy"], t["exchange"], t["start"], t["end"]): ("drift", t)
                             for t in drifts})
                futs.update({pool.submit(runner.backtest, t["strategy"], t["exchange"], t["start"], t["end"]): ("chunk", t)
                             for t in todo})
                try:
                    for fut in as_completed(futs):
                        what, task = futs[fut]
                        try:
                            res = fut.result()
                        except Exception as e:  # a runner bug in one task must not lose the others
                            res = {"ok": False, "has_bias": None, "error": f"{type(e).__name__}: {e}"}
                        if what == "bias":
                            store_bias(conn, task[0], task[1], task[2], res)
                            continue
                        if what == "drift":
                            store_drift(conn, task, res)
                            continue
                        store_chunk(conn, task, res, run_id)
                        done += 1
                        if res.get("no_data"):
                            empty += 1
                        elif not res["ok"]:
                            failed += 1
                            if len(failures) < 100:
                                failures.append(f"{task['strategy']} {task['exchange']} {task['start'].year}: {res['error']}")
                        if done % 25 == 0 or done == len(todo):
                            print(f"analyze: {done}/{len(todo)} chunks, {failed} failed, {empty} with no candles", flush=True)
                except BaseException:
                    # leaving the with block waits for every queued task, hours of work whose results cannot be stored
                    pool.shutdown(cancel_futures=True)
                    raise
            summary.update(chunks_failed=failed, chunks_empty=empty, failures=failures, bias_failed=conn.execute(
                "SELECT count(*) FROM bias_checks WHERE has_bias IS NULL").fetchone()[0])
            summary.update(derive(conn))
            summary.update(suggest.run(conn))
            ok = failed <= FAIL_SHARE * max(len(todo), 1)
            print(f"analyze: done: {json.dumps({k: v for k, v in summary.items() if k not in ('failures', 'skipped')})}",
                  flush=True)
        except Exception as e:
            summary["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s",
                         (Jsonb(summary), ok, run_id))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "bias-one":
        path = sys.argv[sys.argv.index("--strategy-path") + 1] if "--strategy-path" in sys.argv else None
        print("BIAS_RESULT " + json.dumps(bias_one(sys.argv[2], sys.argv[3], path), default=str), flush=True)
        sys.exit(0)
    sys.exit(main())
