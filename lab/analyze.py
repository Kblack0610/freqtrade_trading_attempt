#!/usr/bin/env python3
"""Nightly analysis: every strategy on every exchange's full price history, and what follows from it.

1. Strategies: every file the resolver finds under user_data/strategies, with its hash, into
   `strategies`.
2. Backtests: each strategy x exchange over its whole history, one chunk per past calendar year and per month of the
   current year, run in
   parallel (up to ANALYZE_WORKERS freqtrade processes, fewer when their estimated memory would pass
   ANALYZE_MEMORY_GB: a year of 1m candles needs about four times a year of 5m). A chunk is cached by its cache_key (strategy file,
   config files, Freqtrade version, range, and a digest of that year's candles at the strategy's timeframe):
   past years and past months run once, again only when their candles change (Kraken's quarterly archive lands after
   its year ends), and only the current month reruns each night. On January 1 the year just ended becomes one chunk,
   which replaces its months.
   Its trades go to `backtest_trades`. A chunk starts from the config's wallet, so a long history is
   many short compounding runs chained by their daily returns, not one run that compounds for years.
   Then setup overlays (lab/variants.toml): each strategy x exchange again under every overlay, the overlay's
   config in its cache key, up to [sweep] max_chunks a night, best 365-day Sharpe low end first, and only for a
   strategy x exchange walk-forward has scored.
   A FreqAI strategy (a freqai.json beside its file) runs as-is only, over the last ML_DAYS from the first of that
   month, with that file as its config and a model identifier per venue, chunk and file version, so FreqAI reuses
   only models trained for that chunk (user_data/models/<identifier>). On an image without lightgbm (the plain
   trader image) it is recorded with load_error NEEDS_ML and runs nothing.
3. Bias: lookahead-analysis and recursive-analysis per strategy, rerun when its file hash changes. A FreqAI
   strategy gets none yet: lookahead-analysis would retrain per signal, and which check stands in is open.
4. Derived, rebuilt in one transaction from the stored trades: daily returns, metrics over 30d, 90d,
   180d, 365d and all (with bootstrap intervals and the deflated Sharpe across every strategy x
   exchange), Monte Carlo estimates, BTC regimes, and correlation with the strategies the league runs.
   Each strategy x exchange x overlay gets the same windows in `overlay_windows`, and its series counts in
   the deflated Sharpe of every window; the other derived tables are as-is only.
   Then lab/volatility.py scores HAR against plain variance forecasts per venue and pair (`vol_scores`, `vol_verdict`).
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
import importlib.util
import itertools
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Protocol

import pandas as pd
from psycopg.types.json import Jsonb

from lab import metrics, book, confidence, loop, mixtures, scorecard, suggest, universe, variants, volatility, vol_target, horizon
from lab.db import connect
from lab.util import finite, utc

REPO = Path(os.environ.get("REPO", "/repo"))
REVISION = os.environ.get("TRADER_REVISION", "dev")  # the commit the image was built from; its GHCR tag is sha-<7 chars>
RESULTS = Path(os.environ.get("LAB_RESULTS", "/lab"))
USER_DATA = REPO / "user_data"
EXCHANGES = os.environ.get("ANALYZE_EXCHANGES", "binanceus coinbase").split()
ONLY = set(os.environ.get("ANALYZE_STRATEGIES", "").split())
WORKERS = int(os.environ.get("ANALYZE_WORKERS", "6"))
# What the freqtrade processes in flight may hold together, under the pod's memory limit with room for this one.
MEMORY_GB = float(os.environ.get("ANALYZE_MEMORY_GB", "10"))
BIAS_EXCHANGE = os.environ.get("BIAS_EXCHANGE", "binanceus")
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "120"))
# A slow timeframe's window grows to this many candles: 120 daily candles hold too few entries for the check.
LOOKAHEAD_CANDLES = int(os.environ.get("LOOKAHEAD_CANDLES", "900"))
RECURSIVE_DAYS = int(os.environ.get("RECURSIVE_DAYS", "30"))
RECURSIVE_TOL = float(os.environ.get("RECURSIVE_TOL", "0.001"))  # relative indicator change still called converged
REGIME_EXCHANGE = os.environ.get("REGIME_EXCHANGE", "coinbase")
CHUNK_TIMEOUT = int(os.environ.get("CHUNK_TIMEOUT", "3600"))
MAX_ATTEMPTS = 3
WINDOWS = {"30d": 30, "90d": 90, "180d": 180, "365d": 365, "all": None}
OVERFIT_BLOCKS = 16  # CSCV splits: C(16, 8) = 12,870 in-sample halves
OVERFIT_MIN_DAYS = 90  # under this a block is a handful of days
OVERFIT_COVERAGE = 0.9  # share of a window's days a strategy needs to be ranked in it
HORIZONS = (30, 90)
ESTIMATE_BASIS, ESTIMATE_MIN_DAYS = 365, 180
CORR_DAYS, CORR_MIN_DAYS = 365, 60
FAIL_SHARE = 0.10
# A task's peak memory grows with the candles it loads. Measured in the image (2026-11-05): a year of 5m on
# 10 pairs peaks at 1.1 GB, a year of 1m on 10 pairs at 4.0 GB.
TASK_BASE_GB, GB_PER_MCANDLE = 0.4, 0.69
BIAS_LOAD = 2  # lookahead-analysis keeps the full frame and a sliced copy per signal
NO_DATA = "no candles in this period"
ML_DAYS = int(os.environ.get("ML_DAYS", "365"))  # a FreqAI strategy's window: every 7-day window fits a model per pair
FREQAI = importlib.util.find_spec("lightgbm") is not None  # only the trader-ml image (freqtrade's _freqai base) has it
NEEDS_ML = "needs the trader-ml image"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_files(exchange: str) -> list[Path]:
    return [USER_DATA / "config.base.json", USER_DATA / "exchanges" / f"{exchange}.json"]


def wallet() -> float:
    return float(json.loads((USER_DATA / "config.base.json").read_text()).get("dry_run_wallet", 1000))


@cache
def pair_list(exchange: str) -> tuple[str, ...]:
    """The venue's whitelist in its file order."""
    return tuple(json.loads((USER_DATA / "exchanges" / f"{exchange}.json").read_text())["exchange"]["pair_whitelist"])


def whitelist(exchange: str) -> frozenset[str]:
    return frozenset(pair_list(exchange))


def overlay_config(exchange: str, params: dict | None) -> dict | None:
    """The freqtrade config an overlay layers over this venue's two files; None for the strategy as it stands."""
    return variants.config(params, list(pair_list(exchange))) if params else None


@cache
def freqai_fragment(file: str) -> dict | None:
    """The FreqAI config of a strategy (file relative to REPO): the freqai.json beside it; None for a plain strategy."""
    p = (REPO / file).parent / "freqai.json"
    return json.loads(p.read_text()) if p.exists() else None


def ml_chunks(since: dt.date, today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """A FreqAI strategy's chunks: chunks() over the last ML_DAYS, from the first of that month. FreqAI anchors
    its training windows at a chunk's start, so a start that moves monthly, not nightly, keeps its models reusable."""
    lo = max(since, ml_start(today))
    return [(max(a, lo), b) for a, b in chunks(lo, today)]


def ml_start(today: dt.date) -> dt.date:
    return (today - dt.timedelta(days=ML_DAYS)).replace(day=1)


def chunk_config(s: dict, exchange: str, start: dt.date, params: dict | None = None) -> dict | None:
    """The config a chunk of strategy row s layers over the venue's two files: a FreqAI strategy's fragment with its
    own identifier, else overlay_config()'s."""
    frag = freqai_fragment(s["file"])
    if not frag:
        return overlay_config(exchange, params)
    digest = sha256((s["file_hash"] + json.dumps(frag, sort_keys=True)).encode())[:8]
    return {**frag, "freqai": {**frag["freqai"], "identifier": f"{s['name']}-{exchange}-{start:%Y%m%d}-{digest}"}}


def pair_count(exchange: str) -> int:
    return len(whitelist(exchange))


def lookahead_days(timeframe: str | None) -> int:
    from freqtrade.exchange import timeframe_to_seconds

    return max(LOOKAHEAD_DAYS, math.ceil(LOOKAHEAD_CANDLES * timeframe_to_seconds(timeframe or "5m") / 86400))


def task_gb(exchange: str, timeframe: str | None, days: int, load: float = 1) -> float:
    from freqtrade.exchange import timeframe_to_seconds

    candles = pair_count(exchange) * days * 86400 / timeframe_to_seconds(timeframe or "5m")
    return TASK_BASE_GB + GB_PER_MCANDLE * load * candles / 1e6


class Budget:
    """Lets tasks run while their estimated memory together stays within a total. A task estimated above
    the total runs alone, so nothing waits forever."""

    def __init__(self, total: float):
        self.total, self.used, self.waiting = total, 0.0, 0
        self.cv = threading.Condition()

    @contextmanager
    def hold(self, gb: float):
        gb = min(gb, self.total)
        with self.cv:
            self.waiting += 1
            self.cv.notify_all()
            self.cv.wait_for(lambda: self.used + gb <= self.total)
            self.waiting -= 1
            self.used += gb
        try:
            yield
        finally:
            with self.cv:
                self.used -= gb
                self.cv.notify_all()


def config_digest(exchange: str) -> str:
    """The digests of both config files a run on this exchange loads."""
    return "".join(sha256(p.read_bytes()) for p in config_files(exchange))


def chunk_key(file_hash: str, exchange: str, start: dt.date, end: dt.date, ft_version: str, candles: str,
              overlay: dict | None = None) -> str:
    """overlay is overlay_config()'s; an as-is chunk leaves it out, so its key is the one stored before overlays."""
    parts = [file_hash, config_digest(exchange), ft_version, exchange, str(start), str(end), candles]
    return sha256(json.dumps(parts + [overlay] if overlay else parts, sort_keys=True).encode())


def candle_digests(exchange: str, timeframe: str, by: str, today: dt.date | None = None) -> dict:
    """A digest of the whitelisted pairs' candles at this timeframe per chunks() chunk, keyed by its start date
    (by="chunk", which needs today: each past year whole, then each month of today's year), or per UTC date
    (by="day"). A past year's digest is the one its chunk was stored with before months, so those keys did not move.
    Informative timeframes a strategy also loads are not covered."""
    from freqtrade.misc import pair_to_filename

    out = {}
    for pair in sorted(whitelist(exchange)):
        path = RESULTS / "data" / exchange / f"{pair_to_filename(pair)}-{timeframe}.feather"
        if not path.exists():
            continue
        df = pd.read_feather(path)
        d = df["date"].dt
        if by == "day":
            groups = iter(df.groupby(d.date))
        else:
            assert by == "chunk" and today is not None, 'by="chunk" needs today'
            past = d.year < today.year
            groups = itertools.chain(
                ((dt.date(int(y), 1, 1), rows) for y, rows in df[past].groupby(d.year[past])),
                ((dt.date(today.year, int(m), 1), rows) for m, rows in df[~past].groupby(d.month[~past])))
        for k, rows in groups:
            out.setdefault(k, hashlib.sha256()).update(
                pair.encode() + pd.util.hash_pandas_object(rows, index=False).values.tobytes())
    return {k: h.hexdigest() for k, h in out.items()}


def detail_coverage(exchange: str, timeframe: str, start: dt.date, end: dt.date) -> str | None:
    """Require every intrabar candle on every whitelist pair; partial detail data must not pass as a replay."""
    from freqtrade.exchange import timeframe_to_seconds
    from freqtrade.misc import pair_to_filename

    step = timeframe_to_seconds(timeframe)
    expected = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                             freq=pd.Timedelta(seconds=step), inclusive="left")
    for pair in sorted(whitelist(exchange)):
        path = RESULTS / "data" / exchange / f"{pair_to_filename(pair)}-{timeframe}.feather"
        if not path.exists():
            return f"missing {timeframe} candles for {pair}"
        dates = pd.DatetimeIndex(pd.to_datetime(pd.read_feather(path, columns=["date"])["date"], utc=True))
        if not expected.isin(dates).all():
            return f"incomplete {timeframe} candles for {pair} over {start} to {end}"
    return None


def span_digest(days: dict[dt.date, str], start: dt.date, end: dt.date) -> str:
    """One digest over candle_digests(by="day") from start up to, not including, end."""
    return sha256("".join(f"{d}{h}" for d, h in sorted(days.items()) if start <= d < end).encode())


def digest_key(start: dt.date, today: dt.date) -> dt.date:
    """The candle_digests(by="chunk") key of a chunk starting at start: its year's for a past year (a FreqAI chunk may
    start inside it), else its month's."""
    return dt.date(start.year, 1, 1) if start.year < today.year else start.replace(day=1)


def chunks(first: dt.date, today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Calendar years from the first candle's year up to last year, then each month of the current year from the
    first candle's month, the current month ending today. Only the current month's key moves each night, so a night
    reruns about a month per series instead of the year to date."""
    out = [(dt.date(y, 1, 1), dt.date(y + 1, 1, 1)) for y in range(first.year, today.year)]
    for m in range(first.month if first.year == today.year else 1, today.month + 1):
        start = dt.date(today.year, m, 1)
        end = min(dt.date(today.year + m // 12, m % 12 + 1, 1), today)
        if end > start:
            out.append((start, end))
    return out


class Runner(Protocol):
    def version(self) -> str: ...
    def discover(self) -> list[dict]: ...
    def backtest(self, strategy: str, exchange: str, start: dt.date, end: dt.date, overlay: dict | None = None) -> dict: ...
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
            file = str(loc.relative_to(REPO))
            error = "failed to import" if not cls else NEEDS_ML if freqai_fragment(file) and not FREQAI \
                else load_error(loc.read_text(encoding="utf-8"), name)
            out.append({"name": name, "file": file,
                        "file_hash": sha256(loc.read_bytes()),
                        "timeframe": getattr(cls, "timeframe", None) if cls else None,
                        "startup_candle_count": getattr(cls, "startup_candle_count", None) if cls else None,
                        "load_error": error})
        return out

    def backtest(self, strategy: str, exchange: str, start: dt.date, end: dt.date, overlay: dict | None = None,
                 strategy_path: Path | None = None, detail: str | None = None) -> dict:
        """overlay (chunk_config()'s) is layered last, over both config files. strategy_path puts a folder ahead of
        the repo's, so its copy of the strategy (and params file) loads."""
        from freqtrade.data.btanalysis import load_backtest_data, load_backtest_stats

        t0 = time.monotonic()
        if detail:
            error = detail_coverage(exchange, detail, start, end)
            if error:
                return {"ok": False, "error": error, "duration_s": time.monotonic() - t0}
        with tempfile.TemporaryDirectory(prefix="bt-") as d:
            cmd = ["freqtrade", "backtesting", "--userdir", str(USER_DATA), "--recursive-strategy-search",
                   *[a for p in config_files(exchange) for a in ("-c", str(p))],
                   "--datadir", str(RESULTS / "data" / exchange), "--strategy", strategy,
                   "--timerange", f"{start:%Y%m%d}-{end:%Y%m%d}", "--export", "trades",
                   "--backtest-directory", d, "--cache", "none"]
            if overlay:
                (Path(d) / "overlay.json").write_text(json.dumps(overlay))
                cmd += ["-c", str(Path(d) / "overlay.json")]
            if strategy_path:
                cmd += ["--strategy-path", str(strategy_path)]
            if detail:
                cmd += ["--timeframe-detail", detail]
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
                "bt_start": utc(s["backtest_start"]), "bt_end": utc(s["backtest_end"]),
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

    def since(days: int) -> str:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y%m%d") + "-"

    days = LOOKAHEAD_DAYS if kind == "lookahead" else RECURSIVE_DAYS
    argv = [f"{kind}-analysis", "--userdir", str(USER_DATA), "--recursive-strategy-search", *[a for p in config_files(BIAS_EXCHANGE) for a in ("-c", str(p))],
            "--datadir", str(RESULTS / "data" / BIAS_EXCHANGE), "--strategy", strategy, "--timerange", since(days)]
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
        if kind == "lookahead":  # the window is in candles once the strategy's timeframe is known
            days = lookahead_days(getattr(objs[0]["class"], "timeframe", None))
            cfg["timerange"] = since(days)
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


TRADE_COLS = ["pair", "open_date", "close_date", "open_rate", "close_rate", "min_rate", "stake_amount", "profit_ratio",
              "profit_abs", "exit_reason", "enter_tag"]


def store_chunk(conn, task: dict, res: dict, run_id: int) -> None:
    attempts = MAX_ATTEMPTS if res.get("no_data") else task["attempts"] + 1 if not res["ok"] else 1
    with conn.transaction():
        overlay = task.get("overlay", "")
        # every stored chunk of the series this one overlaps: its own last result, the year-to-date chunk its month
        # replaces, or the months a finished year replaces, so no day of the series is counted twice
        conn.execute("DELETE FROM backtest_chunks WHERE strategy = %s AND exchange = %s AND overlay = %s"
                     " AND chunk_start < %s AND chunk_end > %s",
                     (task["strategy"], task["exchange"], overlay, task["end"], task["start"]))
        trades = res.get("trades")
        chunk_id = conn.execute(
            "INSERT INTO backtest_chunks (strategy, exchange, overlay, chunk_start, chunk_end, cache_key, ok, attempts,"
            " error, bt_start, bt_end, trades, start_balance, final_balance, max_drawdown, duration_s, run_id,"
            " revision, config_digest, candles)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (task["strategy"], task["exchange"], overlay, task["start"], task["end"], task["key"], res["ok"], attempts,
             res.get("error"), res.get("bt_start"), res.get("bt_end"), None if trades is None else len(trades),
             res.get("start_balance"), res.get("final_balance"), res.get("max_drawdown"), res.get("duration_s"),
             run_id, REVISION, config_digest(task["exchange"]), task.get("candles"))).fetchone()[0]
        if trades is not None and len(trades):
            with conn.cursor().copy(f"COPY backtest_trades (chunk_id, {', '.join(TRADE_COLS)}) FROM STDIN") as cp:
                for row in trades[TRADE_COLS].itertuples(index=False):
                    cp.write_row([chunk_id, *map(finite, row)])


def store_bias(conn, strategy: str, kind: str, file_hash: str, res: dict) -> None:
    conn.execute(
        "INSERT INTO bias_checks (strategy, kind, file_hash, has_bias, biased_indicators, detail, error, checked_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, now()) ON CONFLICT (strategy, kind) DO UPDATE SET"
        " file_hash = EXCLUDED.file_hash, has_bias = EXCLUDED.has_bias, biased_indicators = EXCLUDED.biased_indicators,"
        " detail = EXCLUDED.detail, error = EXCLUDED.error, checked_at = now()",
        (strategy, kind, file_hash, res.get("has_bias"), res.get("biased_indicators"),
         Jsonb(finite(res["detail"])) if res.get("detail") is not None else None, res.get("error")))


def sync_overlays(conn, overlays: dict[str, dict]) -> None:
    """Record each overlay's params by id; an overlay gone from the toml takes its chunks with it (its trials stay)."""
    with conn.transaction():
        for oid, o in overlays.items():
            conn.execute("INSERT INTO overlays (id, grp, params) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE"
                         " SET grp = EXCLUDED.grp", (oid, o["group"], Jsonb(o["params"])))
        conn.execute("DELETE FROM backtest_chunks WHERE overlay <> '' AND NOT (overlay = ANY(%s))", (list(overlays),))


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
    """The first candle per exchange x timeframe, once every whitelisted pair is backfilled to its oldest candle.
    A venue still mid-backfill would otherwise run its years on the few pairs done so far and cache them as ok."""
    return {(ex, tf): f for ex, tf, f, pairs in conn.execute(
        "SELECT exchange, timeframe, min(first_ts), array_agg(pair) FROM data_coverage "
        "WHERE first_ts IS NOT NULL AND oldest_reached GROUP BY 1, 2") if whitelist(ex) <= set(pairs)}


def plan(conn, found: list[dict], first: dict, today: dt.date, ft_version: str, overlays: dict[str, dict] | None = None,
         max_overlay: int = 0) -> tuple[list[dict], int, list[str], int]:
    """The chunks to run this time, how many are cached, why some strategy x exchange is skipped, and how many
    overlay chunks wait for a later night. As-is chunks come first; then up to max_overlay overlay chunks, only for a
    strategy x exchange walk-forward has scored (`confidence.score`), since no gate reads an overlay of one it has not:
    those never run, best prior score (the as-is 365-day Sharpe low end) first and each series' newest chunk first, then
    stored ones whose key moved, the one stored longest ago first, so refreshing the current year rotates through
    every series instead of holding the cap on the best few."""
    done = {(s, e, o, c): (k, ok, n, at) for s, e, o, c, k, ok, n, at in conn.execute(
        "SELECT strategy, exchange, overlay, chunk_start, cache_key, ok, attempts, chunk_end FROM backtest_chunks")}
    score = {(s, e): lo for s, e, lo in conn.execute(
        "SELECT strategy, exchange, sharpe_lo FROM strategy_windows WHERE win = '365d' AND sharpe_lo IS NOT NULL")}
    scored = {(s, e) for s, e in conn.execute("SELECT strategy, exchange FROM confidence WHERE score IS NOT NULL")}
    todo, over, cached, skipped = [], [], 0, []
    digests: dict[tuple[str, str], dict[int, str]] = {}
    for s in found:
        if s["load_error"] or (ONLY and s["name"] not in ONLY):
            continue
        for ex in EXCHANGES:
            since = first.get((ex, s["timeframe"]))
            if since is None:
                skipped.append(f"{s['name']} on {ex}: {s['timeframe']} not fully backfilled")
                continue
            if (ex, s["timeframe"]) not in digests:
                digests[ex, s["timeframe"]] = candle_digests(ex, s["timeframe"], "chunk", today)
            ml = freqai_fragment(s["file"]) is not None
            for oid, o in [("", None), *([] if ml or (s["name"], ex) not in scored else (overlays or {}).items())]:
                for start, end in (ml_chunks if ml else chunks)(since.date(), today):
                    cfg = chunk_config(s, ex, start, o and o["params"])
                    candles = digests[ex, s["timeframe"]].get(digest_key(start, today), "")
                    key = chunk_key(s["file_hash"], ex, start, end, ft_version, candles, cfg)
                    prior = done.get((s["name"], ex, oid, start))
                    if prior and prior[0] == key and (prior[1] or prior[2] >= MAX_ATTEMPTS):
                        cached += 1
                        continue
                    attempts = prior[2] if prior and prior[0] == key and not prior[1] else 0
                    (over if oid else todo).append(
                        {"strategy": s["name"], "exchange": ex, "start": start, "end": end, "key": key,
                         "attempts": attempts, "candles": candles, "overlay": oid, "config": cfg,
                         "stored": prior and prior[3]})
    # the newest chunks first: they feed every window, so a run cut short still updates the page
    todo.sort(key=lambda t: (-t["start"].toordinal(), t["strategy"], t["exchange"]))
    over.sort(key=lambda t: (t["stored"] is not None, t["stored"] or dt.date.min,
                             -score.get((t["strategy"], t["exchange"]), -math.inf), t["strategy"], t["exchange"],
                             t["overlay"], -t["start"].toordinal()))
    return todo + over[:max_overlay], cached, skipped, max(len(over) - max_overlay, 0)


def prune_ml(conn, found: list[dict], today: dt.date) -> None:
    """A FreqAI strategy's chunks that start before its window: the window moved on, and they would overlap its new first chunk."""
    names = [s["name"] for s in found if freqai_fragment(s["file"])]
    conn.execute("DELETE FROM backtest_chunks WHERE strategy = ANY(%s) AND chunk_start < %s", (names, ml_start(today)))


def bias_todo(conn, found: list[dict], first: dict) -> list[tuple[str, str, str]]:
    """The checks to run; none for a FreqAI strategy (module docstring, step 3)."""
    have = {(s, k): (h, err, at) for s, k, h, err, at in conn.execute(
        "SELECT strategy, kind, file_hash, error, checked_at FROM bias_checks")}
    week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    out = []
    for s in found:
        if s["load_error"] or (ONLY and s["name"] not in ONLY) or (BIAS_EXCHANGE, s["timeframe"]) not in first \
                or freqai_fragment(s["file"]):
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
              "gap_trades", "gaps", "error"]


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
                 [finite(row.get(c)) for c in DRIFT_COLS])


SERIES_COLS = ["open_date", "close_date", "open_rate", "close_rate", "min_rate", "stake_amount", "profit_ratio", "profit_abs"]
SERIES_BATCH = 50_000  # rows per round trip of the server-side cursor


def series_trades(conn, chunk_ids: list[int]) -> pd.DataFrame:
    """Closed trades of the given chunks, streamed in batches into typed frames, dates already UTC."""
    frames = []
    with conn.cursor(name="series_trades") as cur:
        cur.execute(f"SELECT chunk_id, {', '.join(SERIES_COLS)} FROM backtest_trades WHERE chunk_id = ANY(%s)"
                    " ORDER BY chunk_id, close_date", (chunk_ids,))
        while batch := cur.fetchmany(SERIES_BATCH):
            frames.append(pd.DataFrame(batch, columns=["chunk_id", *SERIES_COLS]))
    t = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["chunk_id", *SERIES_COLS])
    for c in ("open_date", "close_date"):
        t[c] = pd.to_datetime(t[c], utc=True)
    return t.astype({c: float for c in SERIES_COLS[2:]})


def load_series(conn, overlays: bool = False):
    """Daily returns and closed trades of every strategy x exchange as it stands (keyed (strategy, exchange)), or with
    overlays=True of every strategy x exchange x overlay (keyed (strategy, exchange, overlay)), from its successful
    chunks. It yields one series at a time, its trades loaded alone, so memory peaks at one series' rows."""
    chunk_rows = conn.execute(
        "SELECT c.id, c.strategy, c.exchange, c.overlay, c.bt_start, c.bt_end, c.start_balance FROM backtest_chunks c"
        " JOIN strategies s ON s.name = c.strategy AND s.load_error IS NULL"
        f" WHERE c.ok AND c.exchange = ANY(%s) AND c.overlay {'<>' if overlays else '='} ''"
        " ORDER BY c.strategy, c.exchange, c.overlay, c.chunk_start", (EXCHANGES,)).fetchall()
    keys: dict[tuple, list] = {}
    for cid, s, e, o, t0, t1, bal in chunk_rows:
        keys.setdefault((s, e, o) if overlays else (s, e), []).append((cid, t0, t1, bal or wallet()))
    with conn.transaction():  # a server-side cursor lives inside one transaction
        for k, chunks in keys.items():
            trades = series_trades(conn, [c[0] for c in chunks])
            by_chunk = dict(tuple(trades.groupby("chunk_id", sort=False)))
            empty = trades.iloc[0:0]
            ps = []
            for cid, t0, t1, bal in chunks:
                t = by_chunk.get(cid, empty).drop(columns="chunk_id")
                t = t.assign(share=t["stake_amount"] / bal)
                ps.append((metrics.daily_returns(t, t0, t1, bal), t))
            daily = pd.concat([d for d, _ in ps], ignore_index=True).drop_duplicates("day", keep="last")
            yield k, (daily.sort_values("day").reset_index(drop=True), pd.concat([t for _, t in ps], ignore_index=True))


def series_windows(key: tuple, daily: pd.DataFrame, trades: pd.DataFrame) -> list[dict]:
    """One series' window rows, each with its window's daily returns as _ret for deflate(). An overlay series
    (key (strategy, exchange, overlay)) seeds by its overlay too; an as-is one keeps the seeds it always had."""
    rows = []
    if daily.empty:
        return rows
    s, e, *o = key
    end = daily["day"].iloc[-1]
    for win, n in WINDOWS.items():
        if n is not None and daily["day"].iloc[0] > end - dt.timedelta(days=n - 1):
            continue  # not that much history
        d = daily if n is None else daily[daily["day"] > end - dt.timedelta(days=n)]
        lo = pd.Timestamp(d["day"].iloc[0], tz="UTC")
        t = trades[trades["close_date"] >= lo]
        m = metrics.window_metrics(d.reset_index(drop=True), t, metrics.seed_of(s, e, win, *o))
        rows.append({"strategy": s, "exchange": e, **({"overlay": o[0]} if o else {}), "win": win, **m,
                     "_ret": pd.Series(d["ret"].to_numpy(dtype=float), index=d["day"])})
    return rows


def deflate(rows: list[dict]) -> None:
    """The deflated Sharpe of every row against every series tried in its window, as-is and overlays alike."""
    for win in WINDOWS:
        trial = [r for r in rows if r["win"] == win]
        rets = pd.DataFrame({(r["strategy"], r["exchange"], r.get("overlay", "")): r["_ret"] for r in trial
                             if r["_sr_daily"] is not None})
        trials = metrics.effective_trials(rets)
        for r in trial:
            r["dsr"] = metrics.deflated_sharpe(r["_sr_daily"], r["days"], r["_skew"], r["_kurt"], trials)
            r["trials"], r["trials_raw"] = trials, len(trial)


def overfit_rows(series: dict) -> list[dict]:
    """PBO per exchange and window over the daily returns of every strategy with that window there.
    A strategy must cover OVERFIT_COVERAGE of the window's days; the days it lacks count as flat."""
    rows = []
    for ex in sorted({e for _, e in series}):
        mine = {s: d for (s, e), (d, _) in series.items() if e == ex and not d.empty}
        if len(mine) < 2:
            continue
        end = max(d["day"].iloc[-1] for d in mine.values())
        for win, n in WINDOWS.items():
            if n is None or n < OVERFIT_MIN_DAYS:
                continue
            lo = end - dt.timedelta(days=n - 1)
            cols = {s: d[d["day"] >= lo].set_index("day")["ret"] for s, d in mine.items()}
            m = pd.DataFrame({s: c for s, c in cols.items() if len(c) >= OVERFIT_COVERAGE * n})
            if m.shape[1] < 2 or len(m) < OVERFIT_MIN_DAYS:
                continue
            r = metrics.pbo(m.fillna(0.0).to_numpy(dtype=float), OVERFIT_BLOCKS)
            if r:
                rows.append({"exchange": ex, "win": win, "strategies": m.shape[1], "days": len(m),
                             "blocks": OVERFIT_BLOCKS, **r})
    return rows


def hold_columns(rows: list[dict], series: dict, close: pd.Series) -> None:
    """Add each window's share of capital in the market and its return over holding that share of BTC."""
    for r in rows:
        hold(r, *series[(r["strategy"], r["exchange"])], close)


def hold(r: dict, daily: pd.DataFrame, trades: pd.DataFrame, close: pd.Series) -> None:
    start = pd.Timestamp(r["start_day"], tz="UTC")
    end = pd.Timestamp(r["end_day"], tz="UTC") + pd.Timedelta(days=1)
    t = trades[trades["close_date"] >= start]
    capital = metrics.capital_exposure(t, start, end)
    seed = metrics.seed_of(r["strategy"], r["exchange"], r["win"], "hold", *([r["overlay"]] if "overlay" in r else []))
    excess = metrics.hold_excess(daily[daily["day"] >= r["start_day"]], close, capital, seed)
    r.update(zip(("hold_excess", "hold_excess_lo", "hold_excess_hi"), excess), capital_exposure=capital)


def overlay_rows(conn, close: pd.Series) -> list[dict]:
    """The windows of every overlay series, measured as they load, so only their daily returns stay in memory."""
    rows = []
    for k, (daily, trades) in load_series(conn, overlays=True):
        for r in series_windows(k, daily, trades):
            hold(r, daily, trades, close)
            rows.append(r)
    return rows


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


def daily_close(coin: str) -> pd.Series:
    path = RESULTS / "data" / REGIME_EXCHANGE / f"{coin}_USD-1d.feather"
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_feather(path, columns=["date", "close"])
    return pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["date"], utc=True).dt.date)


def vol_files(first: dict, timeframe: str) -> dict[str, dict[str, Path]]:
    """Each analysis venue's candle file per whitelisted pair at `timeframe`, once the venue is fully backfilled there."""
    from freqtrade.misc import pair_to_filename

    return {ex: {p: RESULTS / "data" / ex / f"{pair_to_filename(p)}-{timeframe}.feather" for p in sorted(whitelist(ex))}
            for ex in EXCHANGES if (ex, timeframe) in first}


def regime_rows(close: pd.Series, cfg: dict) -> list[dict]:
    if close.empty:
        return []
    c = cfg["confidence"]
    return [finite(row) for row in metrics.regimes(close, c["trend_band"], c["vol_min_days"]).to_dict("records")]


def insert_many(conn, table: str, rows: list[dict], cols: list[str]) -> None:
    if not rows:
        return
    with conn.cursor().copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row([finite(r.get(c)) for c in cols])


WINDOW_COLS = ["strategy", "exchange", "win", "days", "start_day", "end_day", "trades", "total_return", "cagr", "sharpe",
               "sharpe_lo", "sharpe_hi", "sortino", "calmar", "max_drawdown", "profit_factor", "expectancy",
               "expectancy_lo", "expectancy_hi", "gross_expectancy", "gross_expectancy_lo", "gross_expectancy_hi",
               "fee_per_trade", "winrate", "exposure", "worst_open", "capital_exposure", "hold_excess", "hold_excess_lo", "hold_excess_hi",
               "months_profitable", "dsr", "trials", "trials_raw", "trades_needed"]
ESTIMATE_COLS = ["strategy", "exchange", "horizon_days", "p10", "p50", "p90", "prob_loss", "exp_max_drawdown", "paths",
                 "seed", "basis_days"]

BENCHMARK_COLS = ["name", "win", "start_day", "end_day", "total_return", "cagr", "max_drawdown", "after_tax_return", "trades"]


def derive(conn) -> dict:
    series = dict(load_series(conn))
    league = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM bots")}
    daily = [{"strategy": s, "exchange": e, **r} for (s, e), (d, _) in series.items() for r in d.to_dict("records")]
    close = daily_close("BTC")
    windows = [r for k, (d, t) in series.items() for r in series_windows(k, d, t)]
    overlaid = overlay_rows(conn, close)
    deflate(windows + overlaid)
    estimates = estimate_rows(series)
    bench = book.benchmark_rows(close, daily_close("ETH"), suggest.load_config())
    hold_columns(windows, series, close)
    corr, reg, overfit = correlation_rows(series, league), regime_rows(close, suggest.load_config()), overfit_rows(series)
    with conn.transaction():
        conn.execute("TRUNCATE strategy_daily, strategy_windows, overlay_windows, estimates, correlations, regimes, overfit,"
                     " benchmarks")
        insert_many(conn, "strategy_daily", daily, ["strategy", "exchange", "day", "ret", "pnl", "trades"])
        insert_many(conn, "strategy_windows", windows, WINDOW_COLS)
        insert_many(conn, "overlay_windows", overlaid, ["overlay", *WINDOW_COLS])
        insert_many(conn, "estimates", estimates, ESTIMATE_COLS)
        insert_many(conn, "correlations", corr, ["strategy", "exchange", "other", "rho", "days"])
        insert_many(conn, "regimes", reg, ["day", "btc_close", "trend", "vol_30d", "vol_bucket"])
        insert_many(conn, "overfit", overfit, ["exchange", "win", "strategies", "days", "blocks", "splits", "pbo",
                                               "logit_median"])
        insert_many(conn, "benchmarks", bench, BENCHMARK_COLS)
    return {"series": len(series), "daily_rows": len(daily), "windows": len(windows),
            "overlay_series": len({(r["strategy"], r["exchange"], r["overlay"]) for r in overlaid}),
            "overlay_windows": len(overlaid), "estimates": len(estimates),
            "correlations": len(corr), "regime_days": len(reg), "overfit": len(overfit), "benchmarks": len(bench)}


def main(runner: Runner | None = None, today: dt.date | None = None) -> int:
    runner = runner or Cli()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    started = dt.datetime.now(dt.timezone.utc)
    summary: dict = {"exchanges": EXCHANGES, "only": sorted(ONLY), "universe": universe.describe_all(USER_DATA, EXCHANGES)}
    with connect(autocommit=True) as conn:
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('analysis', %s, '{}', %s) RETURNING id",
                              (started.strftime("%Y-%m-%d %H:%M:%S"), started)).fetchone()[0]
        ok = False
        stage_s: dict[str, float] = {}  # wall seconds per step, so a cut shows where the night went
        summary["stage_s"] = stage_s
        clock = time.monotonic()

        def lap(name: str) -> None:
            nonlocal clock
            stage_s[name] = round(time.monotonic() - clock, 1)
            clock = time.monotonic()

        try:
            found = runner.discover()
            sync_strategies(conn, found)
            prune_ml(conn, found, today)
            overlays, sweep = variants.load()
            sync_overlays(conn, overlays)
            version = runner.version()
            first = coverage(conn)
            todo, cached, skipped, waiting = plan(conn, found, first, today, version, overlays, sweep.get("max_chunks", 0))
            bias = bias_todo(conn, found, first)
            drifts = drift_todo(conn, found, today)
            hashes = {s["name"]: s["file_hash"] for s in found}
            lap("plan")
            summary.update(freqtrade=version, strategies=len(found), load_failed=sum(bool(s["load_error"]) for s in found),
                           chunks_cached=cached, chunks_run=len(todo), skipped=skipped, bias_run=len(bias),
                           drift_run=len(drifts), overlays=len(overlays), overlay_chunks_run=sum(bool(t["overlay"]) for t in todo),
                           overlay_chunks_waiting=waiting)
            print(f"analyze: {len(found)} strategies, {len(todo)} chunks to run, {cached} cached, "
                  f"{len(bias)} bias checks, {WORKERS} workers", flush=True)
            failed, empty, done, failures = 0, 0, 0, []
            budget, tf = Budget(MEMORY_GB), {s["name"]: s["timeframe"] for s in found}

            def held(gb, fn, *args):
                with budget.hold(gb):
                    return fn(*args)

            def bt(t):
                gb = task_gb(t["exchange"], tf.get(t["strategy"]), (t["end"] - t["start"]).days)
                return held, gb, runner.backtest, t["strategy"], t["exchange"], t["start"], t["end"], t.get("config")

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = {pool.submit(held, task_gb(BIAS_EXCHANGE, tf.get(s), lookahead_days(tf.get(s)) if kind == "lookahead"
                                                  else RECURSIVE_DAYS, BIAS_LOAD), runner.bias, kind, s):
                        ("bias", (s, kind, h)) for s, kind, h in bias}
                futs.update({pool.submit(*bt(t)): ("drift", t) for t in drifts})
                futs.update({pool.submit(*bt(t)): ("chunk", t) for t in todo})
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
                                failures.append(f"{task['strategy']} {task['exchange']}{' ' + task['overlay'] if task.get('overlay') else ''}"
                                                f" {task['start']}: {res['error']}")
                        if done % 25 == 0 or done == len(todo):
                            print(f"analyze: {done}/{len(todo)} chunks, {failed} failed, {empty} with no candles", flush=True)
                except BaseException:
                    # leaving the with block waits for every queued task, hours of work whose results cannot be stored
                    pool.shutdown(cancel_futures=True)
                    raise
            lap("backtests")
            summary.update(chunks_failed=failed, chunks_empty=empty, failures=failures, bias_failed=conn.execute(
                "SELECT count(*) FROM bias_checks WHERE has_bias IS NULL").fetchone()[0])
            summary.update(derive(conn))
            lap("derive")
            cfg = suggest.load_config()
            summary.update(volatility.run(conn, vol_files(first, cfg["volatility"]["timeframe"]), cfg))
            lap("volatility")
            for step in (vol_target, suggest, confidence, book, mixtures, scorecard, horizon, loop):
                summary.update(step.run(conn))
                lap(step.__name__.removeprefix("lab."))
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
    from lab import reproduce  # the nightly reproduce step, after the night's own results are stored

    code = main()
    sys.exit(max(code, reproduce.main()))
