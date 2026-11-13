#!/usr/bin/env python3
"""Calibration: does the gate stack find a real edge, and does it reject a fake one?

Seeded synthetic candles go through the real nightly analysis, walk-forward and confidence code, so the pass rates
below are the gates' own, not a second scorer's. Settings: [calibration] in lab/suggest.toml.
- Candles (candles()): per pair, hourly log returns from GARCH(1,1) with Student-t innovations (fat tails, volatility
  clustering), plus a planted component: an AR(1) signal (an Ornstein-Uhlenbeck process sampled hourly) carried in
  the candle volume, whose correlation with the next hour's standardized return is signal_ic. Minute candles bridge
  each hour, so the walk-forward's 1m detail replay has data; daily candles give the BTC regimes.
- Strategies (STRATEGY): each reads the signal mixed with its own hashed noise, so its correlation with the next return,
  its edge, is one of `edges`; a null reads only noise, so it enters at random times. Each has two tuned thresholds
  plus ROI and stoploss, so hyperopt has noise to fit, as it does for a real strategy.
- Pipeline: lab/analyze.py and lab/walkforward.py run as subprocesses against the schema SCHEMA (search_path), with a
  temporary REPO holding only the synthetic venue and these strategies, so nothing here enters the trial register,
  the site or the league. The deflated Sharpe and minimum backtest length gates count the production register, the
  multiple-testing burden a real strategy faces.
- Months: confidence.rows() over each strategy's newest `oos_months` test months gives each length from one run.

A universe (one seed) can take more than one run: the walk-forward stops at WF_BUDGET_S and its folds stay cached in
SCHEMA, so the next run carries on. When none are left, each strategy's gates go to `calibration_outcomes`, the pass
rates over every universe so far to `calibration`, and the next run starts a new universe. Every run also writes SPA
and StepM (Hansen 2005, Romano and Wolf 2005, via `arch`) over the production as-is daily returns, a diagnostic only.

The paper gates (dry run, drift, not decayed) need a paper bot and venues agreeing needs a second real venue, so the
`stack` rate is every other gate passing together. A planted edge shows the pipeline finds a clean edge and invents
none; synthetic candles say nothing about whether crypto has one.

Run with `python lab/calibration.py`; the weekly freqtrade-calibration CronJob does.
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
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from lab import confidence, metrics, suggest
from lab.db import connect, url

REPO = Path(__file__).resolve().parents[1]
SCHEMA = os.environ.get("CALIBRATION_SCHEMA", "calibration_scratch")
WORK = Path(os.environ.get("CALIBRATION_WORK") or tempfile.gettempdir()) / "calibration"
TRAIN_DAYS = int(os.environ.get("WF_TRAIN_DAYS", "180"))  # the walk-forward's, which reads the same variable
EXCHANGE = "binanceus"  # whose fees and markets the synthetic venue borrows
PAPER = ("venues agreeing", "drift", "dry run", "not decayed")  # outside the stack: no paper bot, one venue
STACK = "stack"
VOLUME = 1000.0  # volume = VOLUME * exp(signal / 2): positive, and the strategy reads the signal back exactly
START_PRICE = 100.0

STRATEGY = '''
"""Calibration strategy {name} (lab/calibration.py): edge {edge}. Generated; do not edit."""
import hashlib

import numpy as np
import pandas as pd
from scipy.special import ndtri

from freqtrade.strategy import DecimalParameter, IStrategy

RHO, SEED = {rho!r}, {seed!r}
_STEPS = (0x9E3779B97F4A7C15, 0xBF58476D1CE4E5B9, 0x94D049BB133111EB)


def noise(dates: pd.Series, pair: str) -> np.ndarray:
    """A standard normal per candle from its own date, pair and SEED (splitmix64), so no candle sees another."""
    x = ((dates - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy(dtype=np.uint64)
    x ^= np.uint64(int(hashlib.sha256(f"{{pair}}|{{SEED}}".encode()).hexdigest()[:16], 16))
    with np.errstate(over="ignore"):
        x = x + np.uint64(_STEPS[0])
        x = (x ^ (x >> np.uint64(30))) * np.uint64(_STEPS[1])
        x = (x ^ (x >> np.uint64(27))) * np.uint64(_STEPS[2])
        x = x ^ (x >> np.uint64(31))
    return ndtri(((x >> np.uint64(11)).astype(np.float64) + 0.5) / 2.0 ** 53)


class {name}(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {{"0": 0.05}}
    stoploss = -0.05
    startup_candle_count = 30  # the recursive bias check refuses 0 and finds no candles at 1; the feature needs none
    entry_z = DecimalParameter(0.5, 2.5, default=1.5, decimals=1, space="buy")
    exit_z = DecimalParameter(-1.5, 1.0, default=0.0, decimals=1, space="sell")

    def populate_indicators(self, df, metadata):
        signal = 2 * np.log(df["volume"] / {volume!r})
        df["feature"] = RHO * signal + np.sqrt(1 - RHO ** 2) * noise(df["date"], metadata["pair"])
        return df

    def populate_entry_trend(self, df, metadata):
        df.loc[df["feature"] > self.entry_z.value, "enter_long"] = 1
        return df

    def populate_exit_trend(self, df, metadata):
        df.loc[df["feature"] < self.exit_z.value, "exit_long"] = 1
        return df
'''


def settings(cfg: dict | None = None) -> dict:
    return (cfg or suggest.load_config())["calibration"]


def span(cfg: dict, today: dt.date) -> tuple[dt.date, dt.date]:
    """The candles' first and end day. They end today, as a real venue's do (the bias checks and the 365-day windows
    read back from today); they start one training span before the longest test span before the lockbox, and at least
    366 days before the lockbox, so the 365-day window, which picks the walk-forward's candidates, is full."""
    lockbox = cfg["lockbox"]["start"]
    first = lockbox
    for _ in range(max(cfg["calibration"]["oos_months"])):
        first = (first - dt.timedelta(days=1)).replace(day=1)
    return min(first - dt.timedelta(days=TRAIN_DAYS), lockbox - dt.timedelta(days=366)), today


def universe(seed: int, c: dict) -> list[dict]:
    """The strategies of one universe: nulls first, then per_edge of each planted edge."""
    edges = [0.0] * c["nulls"] + [e for e in c["edges"] for _ in range(c["per_edge"])]
    if any(e > c["signal_ic"] for e in edges):
        raise ValueError("an edge cannot pass signal_ic: it is the signal's correlation times the strategy's share of it")
    out, n = [], {}
    for e in edges:
        n[e] = n.get(e, 0) + 1
        name = f"Cal{round(e * 1000):03d}x{n[e]}"
        out.append({"name": name, "edge": e, "rho": e / c["signal_ic"], "seed": metrics.seed_of(seed, name)})
    return out


def candles(seed: int, pair: str, start: dt.date, end: dt.date, c: dict) -> dict[str, pd.DataFrame]:
    """1m, 1h and 1d candles from start up to end for one pair; the signal sits in the hourly volume. Each day draws
    from its own generator, so the candles up to a day stay the same as end moves on and a resumed universe reruns
    only the newest backtest chunk, as on a real venue."""
    key = int(hashlib.sha256(pair.encode()).hexdigest()[:8], 16)
    days = (end - start).days
    n, phi, ic, df = days * 24, c["signal_phi"], c["signal_ic"], c["tail_df"]
    hours = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq="h")
    raw, eps, z, wick = np.empty(n), np.empty(n), np.empty((n, 60)), np.empty((n, 60, 2))
    for d in range(days):
        rng = np.random.default_rng([seed, key, (start + dt.timedelta(days=d)).toordinal()])
        raw[d * 24:(d + 1) * 24] = rng.standard_normal(24)
        eps[d * 24:(d + 1) * 24] = rng.standard_t(df, 24) / math.sqrt(df / (df - 2))
        z[d * 24:(d + 1) * 24] = rng.standard_normal((24, 60))
        wick[d * 24:(d + 1) * 24] = np.abs(rng.standard_normal((24, 60, 2)))
    signal = np.empty(n)
    signal[0] = raw[0]
    for i in range(1, n):
        signal[i] = phi * signal[i - 1] + math.sqrt(1 - phi ** 2) * raw[i]
    a, b = c["garch_alpha"], c["garch_beta"]
    var0 = c["annual_vol"] ** 2 / (365 * 24)
    omega = var0 * (1 - a - b)
    ret, var = np.empty(n), np.empty(n)
    v, prev = var0, 0.0
    for i in range(n):
        v = omega + a * prev ** 2 + b * v if i else var0
        mean = ic * signal[i - 1] if i else 0.0
        prev = math.sqrt(v) * (mean + math.sqrt(1 - ic ** 2) * eps[i])
        ret[i], var[i] = prev, v
    # each hour's 60 minutes: noise around a straight line, summing to the hour's return
    z -= z.mean(axis=1, keepdims=True)
    steps = z * np.sqrt(var / 60)[:, None] + ret[:, None] / 60
    close = math.log(START_PRICE) + np.cumsum(steps.ravel())
    opens = np.concatenate([[math.log(START_PRICE)], close[:-1]])
    o, cl = np.exp(opens), np.exp(close)
    # every minute trades a little above and below its open and close, as a real one does: an hour that opened at its
    # very high or low would put a limit entry at the open on the fill boundary, and freqtrade would fill it a candle
    # later, which the lookahead check then reads as bias when it cuts the data off after the entry candle
    spread = np.sqrt(var / 60)[:, None, None] * wick / 2
    hi, lo = np.exp(np.maximum(opens, close) + spread[..., 0].ravel()), np.exp(np.minimum(opens, close) - spread[..., 1].ravel())
    volume = VOLUME * np.exp(signal / 2)
    minutes = pd.DataFrame({"date": pd.date_range(hours[0], periods=n * 60, freq="min"), "open": o,
                            "high": hi, "low": lo, "close": cl,
                            "volume": np.repeat(volume / 60, 60)})
    by = minutes[["open", "high", "low", "close"]].to_numpy().reshape(n, 60, 4)
    hourly = pd.DataFrame({"date": hours, "open": by[:, 0, 0], "high": by[:, :, 1].max(axis=1),
                           "low": by[:, :, 2].min(axis=1), "close": by[:, -1, 3], "volume": volume})
    hb = hourly[["open", "high", "low", "close", "volume"]].to_numpy().reshape(days, 24, 5)
    daily = pd.DataFrame({"date": hours[::24], "open": hb[:, 0, 0], "high": hb[:, :, 1].max(axis=1),
                          "low": hb[:, :, 2].min(axis=1), "close": hb[:, -1, 3], "volume": hb[:, :, 4].sum(axis=1)})
    return {"1m": minutes, "1h": hourly, "1d": daily}


def write_repo(root: Path, strategies: list[dict], c: dict) -> Path:
    """A REPO with the real base config, the synthetic venue (EXCHANGE's fees, only `pairs`) and the strategies."""
    ud = root / "user_data"
    (ud / "exchanges").mkdir(parents=True, exist_ok=True)
    (ud / "strategies").mkdir(exist_ok=True)
    (ud / "config.base.json").write_text((REPO / "user_data" / "config.base.json").read_text())
    venue = json.loads((REPO / "user_data" / "exchanges" / f"{EXCHANGE}.json").read_text())
    venue["exchange"]["pair_whitelist"] = list(c["pairs"])
    (ud / "exchanges" / f"{EXCHANGE}.json").write_text(json.dumps(venue, indent=2))
    for s in strategies:
        (ud / "strategies" / f"{s['name']}.py").write_text(STRATEGY.format(volume=VOLUME, **s))
    return root


def write_candles(data: Path, seed: int, start: dt.date, end: dt.date, c: dict) -> dict[tuple[str, str], tuple]:
    """Every pair's candle files; (pair, timeframe) -> (first, last, rows) for data_coverage."""
    from freqtrade.misc import pair_to_filename

    data.mkdir(parents=True, exist_ok=True)
    out = {}
    for pair in c["pairs"]:
        for tf, df in candles(seed, pair, start, end, c).items():
            df.to_feather(data / f"{pair_to_filename(pair)}-{tf}.feather")
            out[pair, tf] = (df["date"].iloc[0].to_pydatetime(), df["date"].iloc[-1].to_pydatetime(), len(df))
    return out


def scratch_url() -> str:
    return psycopg.conninfo.make_conninfo(url(), options=f"-csearch_path={SCHEMA}")


def scratch_env(repo: Path, results: Path, n: int, folds: int) -> dict:
    """What analysis and walk-forward read: the temporary REPO, its candles and the scratch schema, one venue, the
    newest `folds` test months."""
    return {**os.environ, "REPO": str(repo), "LAB_RESULTS": str(results), "FT_USER_DATA": str(repo / "user_data"),
            "DATABASE_URL": scratch_url(), "ANALYZE_EXCHANGES": EXCHANGE, "REGIME_EXCHANGE": EXCHANGE,
            "BIAS_EXCHANGE": EXCHANGE, "ANALYZE_STRATEGIES": "", "WF_EXCHANGES": EXCHANGE, "WF_STRATEGIES": "",
            "WF_LIMIT": str(n), "WF_FOLDS": str(folds), "WF_SHARDS": "1", "JOB_COMPLETION_INDEX": "0", "WF_JOB": "",
            "PYTHONPATH": str(REPO)}


def digest(c: dict) -> str:
    return hashlib.sha256(json.dumps(c, sort_keys=True, default=str).encode()).hexdigest()


def open_universe(conn, cfg: dict, today: dt.date) -> dict:
    """The universe in progress, or a new one in a freshly migrated scratch schema when the last was recorded,
    the settings changed, or there is none."""
    c, key = cfg["calibration"], digest({**cfg["calibration"], "lockbox": cfg["lockbox"], "train_days": TRAIN_DAYS})
    exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s "
                          "AND table_name = 'calibration_universe'", (SCHEMA,)).fetchone()
    row = conn.execute(f"SELECT tag, seed, settings FROM {SCHEMA}.calibration_universe WHERE recorded_at IS NULL"
                       ).fetchone() if exists else None
    if row and row[2] == key:
        return {"tag": row[0], "seed": row[1], "resumed": True}
    n = conn.execute("SELECT count(DISTINCT run) FROM calibration_outcomes").fetchone()[0]
    tag = f"{n + 1:04d}-{today:%G-W%V}"  # numbered, so two universes recorded in one week still differ
    seed = metrics.seed_of("calibration", tag)
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    subprocess.run([sys.executable, str(REPO / "lab" / "migrate.py")], check=True,
                   env={**os.environ, "DATABASE_URL": scratch_url()})
    conn.execute(f"CREATE TABLE {SCHEMA}.calibration_universe (tag text PRIMARY KEY, seed bigint NOT NULL, "
                 "settings text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), recorded_at timestamptz)")
    conn.execute(f"INSERT INTO {SCHEMA}.calibration_universe (tag, seed, settings) VALUES (%s, %s, %s)", (tag, seed, key))
    return {"tag": tag, "seed": seed, "resumed": False}


def cover(conn, spans: dict[tuple[str, str], tuple]) -> None:
    for (pair, tf), (first, last, rows) in spans.items():
        conn.execute(f"INSERT INTO {SCHEMA}.data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, "
                     "oldest_reached) VALUES (%s, %s, %s, %s, %s, %s, true) ON CONFLICT (exchange, pair, timeframe) "
                     "DO UPDATE SET first_ts = EXCLUDED.first_ts, last_ts = EXCLUDED.last_ts, rows = EXCLUDED.rows",
                     (EXCHANGE, pair, tf, first, last, rows))


def step(name: str, code: str, env: dict, report: dict) -> int:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], env=env)
    report.setdefault("stage_s", {})[name] = round(time.monotonic() - t0, 1)
    return p.returncode


def gate_name(g: dict) -> str:
    return g["gate"].split(",")[0]  # "deflated Sharpe, 4100 trials" counts with every other deflated Sharpe


def outcomes(facts: dict, trials: int, cfg: dict, strategies: list[dict], months: int) -> list[dict]:
    """Each universe strategy's gates over its newest `months` test months, through confidence.rows()."""
    edge = {s["name"]: s["edge"] for s in strategies}
    trimmed = {**facts, "walkforward": {k: v[-months:] for k, v in facts["walkforward"].items()}}
    out = []
    for r in confidence.rows(trimmed, trials, cfg):
        if r["strategy"] not in edge:
            continue
        gates = {gate_name(g): bool(g["pass"]) for g in r["gates"].obj}
        out.append({"strategy": r["strategy"], "edge": edge[r["strategy"]], "oos_months": months,
                    "ready": all(v for k, v in gates.items() if k not in PAPER), "gates": gates,
                    "oos_trades": r["oos_trades"], "oos_expectancy": r["oos_expectancy"], "score": r["score"]})
    return out


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if not n:
        return None, None
    p = passed / n
    mid, half = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (mid - half) / (1 + z * z / n), (mid + half) / (1 + z * z / n)


def table(rows: list[tuple]) -> list[dict]:
    """Pass rates per gate (and the stack) per edge and test length, over (run, edge, months, ready, gates) rows.
    At edge 0 the rate is the false pass rate; above it, the power."""
    counts: dict = {}
    for run, edge, months, ready, gates in rows:
        for name, ok in [*gates.items(), (STACK, ready)]:
            c = counts.setdefault((name, edge, months), {"n": 0, "passed": 0, "runs": set()})
            c["n"] += 1
            c["passed"] += bool(ok)
            c["runs"].add(run)
    out = []
    for (name, edge, months), c in sorted(counts.items()):
        lo, hi = wilson(c["passed"], c["n"])
        out.append({"gate": name, "edge": edge, "oos_months": months, "runs": len(c["runs"]), "n": c["n"],
                    "passed": c["passed"], "rate": c["passed"] / c["n"], "lo": lo, "hi": hi})
    return out


def record(conn, tag: str, rows: list[dict]) -> int:
    with conn.transaction():
        conn.execute("DELETE FROM calibration_outcomes WHERE run = %s", (tag,))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO calibration_outcomes (run, strategy, edge, oos_months, ready, gates, oos_trades, "
                            "oos_expectancy, score) VALUES (%(run)s, %(strategy)s, %(edge)s, %(oos_months)s, %(ready)s, "
                            "%(gates)s, %(oos_trades)s, %(oos_expectancy)s, %(score)s)",
                            [{**r, "run": tag, "gates": Jsonb(r["gates"])} for r in rows])
        agg = table(conn.execute("SELECT run, edge, oos_months, ready, gates FROM calibration_outcomes").fetchall())
        conn.execute("DELETE FROM calibration")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO calibration (gate, edge, oos_months, runs, n, passed, rate, lo, hi) VALUES "
                            "(%(gate)s, %(edge)s, %(oos_months)s, %(runs)s, %(n)s, %(passed)s, %(rate)s, %(lo)s, %(hi)s)",
                            agg)
    return len(agg)


def spa(conn, cfg: dict, days: int = 365, reps: int = 1000) -> dict:
    """SPA and StepM per venue over the as-is daily net returns of every strategy with a full year before the lockbox,
    against cash: a random entry's expectancy is minus its costs, so cash is the stricter benchmark. A diagnostic."""
    from arch.bootstrap import SPA, StepM

    end = cfg["lockbox"]["start"]
    start = end - dt.timedelta(days=days)
    df = pd.DataFrame(conn.execute("SELECT strategy, exchange, day, ret FROM strategy_daily WHERE day >= %s AND day < %s",
                                   (start, end)).fetchall(), columns=["strategy", "exchange", "day", "ret"])
    out = {}
    for ex, g in df.groupby("exchange"):
        m = g.pivot_table(index="day", columns="strategy", values="ret").reindex(pd.date_range(start, end, inclusive="left").date)
        m = m.loc[:, m.notna().mean() >= 0.9].fillna(0.0).astype(float)
        if m.shape[1] < 2:
            continue
        losses, bench = -m, np.zeros(len(m))
        seed = metrics.seed_of("spa", ex, end)
        s = SPA(bench, losses, block_size=10, reps=reps, seed=seed)
        s.compute()
        w = StepM(bench, losses, size=0.05, block_size=10, reps=reps, seed=seed)
        w.compute()
        out[ex] = {"strategies": m.shape[1], "days": len(m), "spa": {k: float(v) for k, v in s.pvalues.items()},
                   "stepm": sorted(map(str, w.superior_models))[:50]}
    return out


def run(conn, cfg: dict, today: dt.date, report: dict) -> bool:
    c = cfg["calibration"]
    u = open_universe(conn, cfg, today)
    strategies = universe(u["seed"], c)
    start, end = span(cfg, today)
    report.update(universe=u["tag"], resumed=u["resumed"], strategies=len(strategies), start=str(start), end=str(end))
    print(f"calibration: universe {u['tag']} ({'resumed' if u['resumed'] else 'new'}), {len(strategies)} strategies, "
          f"{start} to {end}", flush=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cal-", dir=WORK) as d:
        repo, results = write_repo(Path(d) / "repo", strategies, c), Path(d) / "lab"
        t0 = time.monotonic()
        cover_spans = write_candles(results / "data" / EXCHANGE, u["seed"], start, end, c)
        report.setdefault("stage_s", {})["candles"] = round(time.monotonic() - t0, 1)
        with psycopg.connect(scratch_url(), autocommit=True) as sc:
            cover(sc, cover_spans)
        env = scratch_env(repo, results, len(strategies), max(c["oos_months"]))
        analysed = step("analysis", "import sys; from lab import analyze; sys.exit(analyze.main())", env, report)
        walked = step("walkforward", "import sys; from lab import walkforward; sys.exit(walkforward.main())", env, report)
    with psycopg.connect(scratch_url(), autocommit=True) as sc:
        wf = sc.execute("SELECT raw FROM runs WHERE kind = 'walkforward' ORDER BY id DESC LIMIT 1").fetchone()
        wf = wf[0] if wf else {}
        report.update(analysis_exit=analysed, walkforward_exit=walked,
                      folds={k: wf.get(k) for k in ("candidates", "folds_run", "folds_cached", "folds_failed", "folds_left")})
        report["folds"]["candidates"] = len(wf.get("candidates") or [])
        # the walk-forward's own post steps (suggest, book, ...) may fail on a venue with no bots; only its folds count
        done = bool(wf.get("candidates")) and not wf.get("folds_left")
        if done:
            scratch_trials = confidence.register(sc)
            facts = confidence.load(sc, cfg)
    if wf and not wf.get("candidates"):
        raise RuntimeError("the walk-forward found no candidates: no strategy has a 365-day window in the scratch schema")
    if not done:
        print("calibration: folds left; the next run carries this universe on", flush=True)
        return analysed == 0 and walked == 0
    production = confidence.trial_count(conn)
    trials = max(production, scratch_trials)
    rows = [r for m in c["oos_months"] for r in outcomes(facts, trials, cfg, strategies, m)]
    report.update(trials=trials, production_trials=production, outcomes=len(rows), table_rows=record(conn, u["tag"], rows))
    with psycopg.connect(scratch_url(), autocommit=True) as sc:
        sc.execute("UPDATE calibration_universe SET recorded_at = now() WHERE tag = %s", (u["tag"],))
    print(f"calibration: recorded {len(rows)} outcomes for universe {u['tag']}", flush=True)
    return analysed == 0 and walked == 0


def main(today: dt.date | None = None) -> int:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    cfg = suggest.load_config()
    with connect(autocommit=True) as conn:
        run_id = conn.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('calibration', %s, '{}', now()) "
                              "RETURNING id", (str(today),)).fetchone()[0]
        report: dict = {}
        ok = False
        try:
            ok = run(conn, cfg, today, report)
            report["spa"] = spa(conn, cfg)
        except Exception as e:
            report["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            ok = False
        finally:
            conn.execute("UPDATE runs SET raw = %s, finished_at = now(), ok = %s WHERE id = %s", (Jsonb(report), ok, run_id))
        print(f"calibration: done: {json.dumps(report, default=str)[:2000]}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
