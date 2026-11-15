#!/usr/bin/env python3
"""Event-to-call rules: tagged feed events become scored paper calls, each rule a registered trial.

  python -m lab.call_rules     # the freqtrade-call-rules CronJob, every 15 minutes

Each run registers every rule in lab/call_rules.toml as a trial ('rule:<name>', exchange 'calls', a hash of its
settings) before it reads a single event, so a rule, or an edit of one, raises the bar every rule is judged against
whether or not it ever makes a call. It then reads feed-ingest's recent items (FEED_DATABASE_URL, the read-only
feed_reader role) and, for each item a rule matches, writes a call with who='rule' through the same entry rule as
`lab.calls add`: it enters at the next regular close still ahead of now, so the call never knows its entry. One call
per rule, ticker, direction and entry close; a later event before that close joins it in call_rule_events. Scoring is
lab/calls.py's, in the nightly analysis.

records() is each rule's scored record per horizon: the deflated Sharpe of its per-call net returns over the count
of every rule trial (lab/metrics.py deflated_sharpe), and whether it clears the [record] bar. A call made here is
never an order and never promotes a bot (docs/operating-rules.md).
"""

import datetime as dt
import hashlib
import json
import os
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

from lab import calls, metrics, taxonomy
from lab.db import connect

CONFIG = Path(__file__).resolve().parent / "call_rules.toml"
EXCHANGE = "calls"
# The settings that decide which calls a rule makes; the name is the trial's strategy, the thesis is prose.
SETTINGS = ("sources", "event_types", "min_abs_sentiment", "signal_source", "horizons")

# feed-ingest's ledger (home-config apps/feed-ingest): watchlist-matched, tagged items, by its own insert clock.
FEED_ITEMS = """
SELECT source, id, ingested_at, url, text, tickers, tags FROM feed_items
WHERE ingested_at >= %s AND cardinality(tickers) > 0 AND tags IS NOT NULL ORDER BY ingested_at, source, id
"""
OPEN_CALL = """
SELECT c.id FROM call_rule_events e JOIN calls c ON c.id = e.call_id
WHERE e.rule = %s AND e.variant = %s AND c.ticker = %s AND c.direction = %s AND c.entry_session = %s LIMIT 1
"""
RULE_SCORES = """
SELECT DISTINCT e.rule, e.variant, s.call_id, s.horizon, s.pnl, s.exit_session
FROM call_rule_events e JOIN call_scores s ON s.call_id = e.call_id ORDER BY s.exit_session, s.call_id
"""


def load_config(path: Path = CONFIG) -> dict:
    cfg = tomllib.loads(path.read_text())
    names = [r["name"] for r in cfg["rules"]]
    if len(set(names)) != len(names):
        raise ValueError("rule names must be unique")
    for r in cfg["rules"]:
        taxonomy.validate({"signal_source": r["signal_source"], "instrument": "equity", "horizon": r["horizons"]})
    return cfg


def variant(rule: dict) -> str:
    return hashlib.md5(json.dumps({k: rule[k] for k in SETTINGS}, sort_keys=True).encode()).hexdigest()


def register(conn, cfg: dict) -> int:
    conn.cursor().executemany("INSERT INTO trials (strategy, exchange, variant) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                              [(f"rule:{r['name']}", EXCHANGE, variant(r)) for r in cfg["rules"]])
    return trial_count(conn)


def trial_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM trials WHERE exchange = %s", (EXCHANGE,)).fetchone()[0]


def matches(rule: dict, item: dict) -> str | None:
    """The call's direction if the rule takes this item, else None."""
    tags = item.get("tags") or {}
    sentiment = float(tags.get("sentiment") or 0.0)
    if item["source"] not in rule["sources"] or tags.get("event_type") not in rule["event_types"]:
        return None
    if abs(sentiment) < rule["min_abs_sentiment"] or sentiment == 0:
        return None
    return "short" if sentiment < 0 else "long"


def items(feed_conn, since: dt.datetime) -> list[dict]:
    cols = ("source", "id", "ingested_at", "url", "text", "tickers", "tags")
    return [dict(zip(cols, r)) for r in feed_conn.execute(FEED_ITEMS, (since,))]


def run(conn, found: list[dict], now: dt.datetime | None = None, cfg: dict | None = None) -> dict:
    """Register every rule, then turn the items a rule matches into calls. Idempotent: an event a rule has seen is
    skipped, so overlapping runs make no second call."""
    cfg = cfg or load_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {"rule_trials": register(conn, cfg), "rule_calls": 0, "rule_events": 0}
    oldest = now - dt.timedelta(hours=cfg["max_age_hours"])
    entry = calls.next_entry(now)
    for rule in cfg["rules"]:
        name, var = rule["name"], variant(rule)
        for item in found:
            direction = matches(rule, item)
            if direction is None or item["ingested_at"] < oldest:
                continue
            for ticker in item["tickers"]:
                seen = conn.execute("SELECT 1 FROM call_rule_events WHERE rule = %s AND variant = %s AND feed_source = %s "
                                    "AND feed_id = %s AND ticker = %s", (name, var, item["source"], item["id"], ticker)).fetchone()
                if seen:
                    continue
                with conn.transaction():
                    row = conn.execute(OPEN_CALL, (name, var, ticker, direction, entry)).fetchone()
                    if row:
                        call_id = row[0]
                    else:
                        t = cfg["tickers"].get(ticker, {})
                        call_id = conn.execute(
                            "INSERT INTO calls (who, author, thesis, ticker, direction, instrument, entry_session, horizons, "
                            "benchmark, source, signal_source) VALUES ('rule', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                            (f"rule:{name}", rule["thesis"], ticker, direction, t.get("instrument", "equity"), entry,
                             sorted(rule["horizons"]), t.get("benchmark", "SPY"), item["url"], rule["signal_source"])).fetchone()[0]
                        out["rule_calls"] += 1
                    conn.execute("INSERT INTO call_rule_events (rule, variant, feed_source, feed_id, ticker, ingested_at, call_id) "
                                 "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                 (name, var, item["source"], item["id"], ticker, item["ingested_at"], call_id))
                out["rule_events"] += 1
    print(f"call_rules: {out}", flush=True)
    return out


def records(rows: list[dict], trials: int, cfg: dict | None = None) -> list[dict]:
    """Each rule variant's record per horizon: scored calls, mean net return, deflated Sharpe over every rule trial,
    and whether it clears the [record] bar."""
    bar = (cfg or load_config())["record"]
    by: dict = {}
    for r in rows:
        by.setdefault((r["rule"], r["variant"], r["horizon"]), []).append(r["pnl"])
    out = []
    for (rule, var, horizon), pnl in sorted(by.items()):
        x = pd.Series(pnl, dtype=float)
        sd = float(x.std()) if len(x) > 1 else 0.0
        dsr = None if sd == 0 else metrics.deflated_sharpe(float(x.mean()) / sd, len(x), float(x.skew()) if len(x) > 2 else 0.0,
                                                           float(x.kurt()) + 3.0 if len(x) > 3 else 3.0, trials)
        out.append({"rule": rule, "variant": var, "horizon": horizon, "n": len(x), "mean": float(np.mean(pnl)),
                    "dsr": dsr, "trials": trials,
                    "edge": len(x) >= bar["min_calls"] and dsr is not None and dsr >= bar["dsr_min"]})
    return out


def main() -> int:
    dsn = os.environ.get("FEED_DATABASE_URL")
    if not dsn:
        print("call_rules: FEED_DATABASE_URL is not set (Secret freqtrade/trader-feeds)", file=sys.stderr)
        return 1
    import psycopg

    cfg = load_config()
    now = dt.datetime.now(dt.timezone.utc)
    with psycopg.connect(dsn, autocommit=True) as feed:
        found = items(feed, now - dt.timedelta(hours=cfg["max_age_hours"]))
    with connect(autocommit=True) as conn:
        run(conn, found, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
