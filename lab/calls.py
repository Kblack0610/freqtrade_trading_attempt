#!/usr/bin/env python3
"""Calls: every trade idea, written down before its outcome and scored after it, whoever made it.

  python -m lab.calls add --who human --author kenneth --ticker NVDA --direction short --benchmark SMH \\
      --thesis "..." [--instrument equity] [--horizons 1,5,20] [--entry YYYY-MM-DD] [--source URL]

A call enters at a session's regular close (by default the next one still to print) and is scored at each horizon,
in trading sessions after it, against a benchmark over the same sessions. `add` refuses an entry close that has
already printed, and the calls table refuses UPDATE and DELETE (migration 048), so the record cannot be flattered.

run(conn) is a step of the nightly analysis and the only writer of call_scores: it reads daily closes for every
symbol with an unscored horizon from Alpaca market data (lab/broker.py AlpacaData, the paper key, GET only) and
scores each horizon whose exit close has printed. pnl is the call's own return after costs (lab/calls.toml): a
short pays borrow on the calendar days held, and both directions pay the per-side cost twice. A horizon whose
exit is past due with no close to score it (no data key, a failed read, a ticker with no bars) counts in
calls_unscored, and the analysis exits 1 for it, so the job's alert fires.

groups() is the scored record per (who, instrument, horizon): the mean net return and its anytime-valid interval
(lab/metrics.py confidence_sequence), as the site's /calls shows it.
"""

import argparse
import datetime as dt
import math
import os
import sys
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from lab import broker, metrics
from lab.db import connect

CONFIG = Path(__file__).resolve().parent / "calls.toml"
ET = ZoneInfo("America/New_York")
CLOSE = dt.time(16, 0)

PENDING = """
SELECT c.id, c.ticker, c.direction, c.entry_session, c.benchmark, h AS horizon
FROM calls c CROSS JOIN LATERAL unnest(c.horizons) h
WHERE NOT EXISTS (SELECT 1 FROM call_scores s WHERE s.call_id = c.id AND s.horizon = h)
ORDER BY c.id, h
"""
SCORED = """
SELECT c.who, c.instrument, s.horizon, s.pnl FROM call_scores s JOIN calls c ON c.id = s.call_id
ORDER BY s.exit_session, s.call_id
"""


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text())


def close_at(session: dt.date) -> dt.datetime:
    return dt.datetime.combine(session, CLOSE, ET)


def next_entry(now: dt.datetime) -> dt.date:
    """The next weekday whose regular close is still ahead of now. A holiday is skipped when scoring: the entry is the
    first session on or after this date."""
    d = now.astimezone(ET).date()
    while d.weekday() >= 5 or close_at(d) <= now:
        d += dt.timedelta(days=1)
    return d


def final(closes: list[tuple[dt.date, float]], now: dt.datetime, settle_h: float) -> list[tuple[dt.date, float]]:
    """Only the sessions whose daily bar is final: settle_h hours after the close, so a bar read mid-session or
    during the closing auction is never scored."""
    return [(d, c) for d, c in closes if now >= close_at(d) + dt.timedelta(hours=settle_h)]


def score(direction: str, entry: dt.date, horizon: int, closes: list, bench: list, cfg: dict) -> dict | None:
    """The call's score at one horizon, or None until its exit close has printed. closes and bench are final
    (session, close) lists, oldest first. Entry is the first session on or after `entry`; exit is `horizon` sessions
    of the ticker after it; the benchmark is read on the same two sessions."""
    after = [i for i, (d, _) in enumerate(closes) if d >= entry]
    if not after or after[0] + horizon >= len(closes):
        return None
    (d0, c0), (d1, c1) = closes[after[0]], closes[after[0] + horizon]
    b = dict(bench)
    if d0 not in b or d1 not in b:
        return None
    sign = 1 if direction == "long" else -1
    ret, bret = c1 / c0 - 1, b[d1] / b[d0] - 1
    borrow = cfg["borrow_apr"] * (d1 - d0).days / 360 if sign < 0 else 0.0
    pnl = sign * ret - borrow - 2 * cfg["cost_bps"] / 1e4
    return {"exit_session": d1, "entry_close": c0, "exit_close": c1, "bench_entry": b[d0], "bench_exit": b[d1],
            "ret": ret, "pnl": pnl, "excess": sign * (ret - bret)}


def overdue(entry: dt.date, horizon: int, today: dt.date, slack_days: int) -> bool:
    """A horizon is due once its calendar span (five sessions a week) plus slack for holidays has passed."""
    return (today - entry).days > math.ceil(horizon * 7 / 5) + slack_days


def client():
    key, secret = os.environ.get("ALPACA_PAPER_KEY_ID"), os.environ.get("ALPACA_PAPER_SECRET_KEY")
    if not (key and secret):
        return None
    return broker.AlpacaData(tomllib.loads(broker.CONFIG.read_text())["alpaca_data"], key, secret)


def run(conn, data=None, now: dt.datetime | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(ET).date()
    pending = [dict(zip(("id", "ticker", "direction", "entry", "benchmark", "horizon"), r))
               for r in conn.execute(PENDING)]
    out = {"calls_scored": 0, "calls_pending": 0, "calls_unscored": 0, "calls_error": None}
    if not pending:
        return out
    data = data if data is not None else client()
    closes: dict = {}
    if data is None:
        out["calls_error"] = "no ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY"
    else:
        symbols = sorted({p["ticker"] for p in pending} | {p["benchmark"] for p in pending})
        try:
            closes = {s: final(v, now, cfg["settle_hours"]) for s, v in
                      data.closes(symbols, min(p["entry"] for p in pending) - dt.timedelta(days=7)).items()}
        except (RuntimeError, KeyError, ValueError, requests.RequestException) as e:
            # a failed read leaves every call pending; the overdue count below fails the job
            out["calls_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    for p in pending:
        s = score(p["direction"], p["entry"], p["horizon"], closes.get(p["ticker"], []),
                  closes.get(p["benchmark"], []), cfg)
        if s is None:
            out["calls_unscored" if overdue(p["entry"], p["horizon"], today, cfg["slack_days"]) else "calls_pending"] += 1
            continue
        conn.execute("INSERT INTO call_scores (call_id, horizon, exit_session, entry_close, exit_close, bench_entry, "
                     "bench_exit, ret, pnl, excess) VALUES (%(id)s, %(horizon)s, %(exit_session)s, %(entry_close)s, "
                     "%(exit_close)s, %(bench_entry)s, %(bench_exit)s, %(ret)s, %(pnl)s, %(excess)s) "
                     "ON CONFLICT DO NOTHING", {**p, **s})
        out["calls_scored"] += 1
    print(f"calls: {out}", flush=True)
    return out


def groups(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    """The scored record per (who, instrument, horizon), in scoring order: count, hit rate, mean net return and its
    anytime-valid interval over [-bound, bound]."""
    cfg = cfg or load_config()
    by: dict = {}
    for r in rows:
        by.setdefault((r["who"], r["instrument"], r["horizon"]), []).append(r["pnl"])
    out = []
    for (who, instrument, horizon), pnl in sorted(by.items()):
        lo, hi = metrics.confidence_sequence(pnl, -cfg["bound"], cfg["bound"], cfg["alpha"])
        out.append({"who": who, "instrument": instrument, "horizon": horizon, "n": len(pnl),
                    "hit": sum(x > 0 for x in pnl) / len(pnl), "mean": sum(pnl) / len(pnl),
                    "lo": float(lo[-1]), "hi": float(hi[-1])})
    return out


def add(conn, a: argparse.Namespace, now: dt.datetime) -> int:
    entry = a.entry or next_entry(now)
    if close_at(entry) <= now:
        raise SystemExit(f"calls: refusing entry {entry}: its close has printed, so the call would know its entry")
    horizons = sorted({int(h) for h in a.horizons.split(",")})
    return conn.execute(
        "INSERT INTO calls (who, author, thesis, ticker, direction, instrument, entry_session, horizons, benchmark, "
        "source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (a.who, a.author, a.thesis, a.ticker.upper(), a.direction, a.instrument, entry, horizons, a.benchmark.upper(),
         a.source)).fetchone()[0]


def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lab.calls")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="record a call before its entry close prints")
    p.add_argument("--who", required=True, choices=("human", "agent", "rule"))
    p.add_argument("--author", required=True)
    p.add_argument("--thesis", required=True)
    p.add_argument("--ticker", required=True)
    p.add_argument("--direction", required=True, choices=("long", "short"))
    p.add_argument("--instrument", default="equity", choices=("equity", "etf"))
    p.add_argument("--benchmark", required=True)
    p.add_argument("--horizons", default="1,5,20")
    p.add_argument("--entry", type=dt.date.fromisoformat)
    p.add_argument("--source")
    a = ap.parse_args(argv)
    now = now or dt.datetime.now(dt.timezone.utc)
    with connect(autocommit=True) as conn:
        call_id = add(conn, a, now)
    print(f"calls: recorded call {call_id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
