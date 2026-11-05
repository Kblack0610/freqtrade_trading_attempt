#!/usr/bin/env python3
"""Whether a strategy on an exchange has earned real money. Advice for a human, like lab/suggest.py: nothing
here touches a bot, and lab/league.py refuses real money whatever this says.

The score is 100 x P(true net expectancy per trade > 0): each strategy's pooled walk-forward result, shrunk
toward the population of everything walk-forward has tried (metrics.shrink). It is one number beside hard gates,
not a blend of them, because no weighting of these gates has any evidence behind it. Ready means every gate in
suggest.toml [confidence] passes, the score included. It means worth a small capped experiment, not safe.

Out of sample means the tuned returns: months hyperopt never saw. So the score is about tuning on the months
before and trading the next, and the walk-forward candidates were themselves chosen on a trailing year.

Every run first adds what was tried to the trial register, `trials`, which keeps a row when its strategy goes.
The effective trial count is the register's size at the window's effective share (metrics.effective_trials).

Run alone with `python lab/confidence.py`; lab/analyze.py and lab/walkforward.py run it after suggest.
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb
from scipy.stats import norm

import metrics
import suggest
from db import connect
from suggest import gate

USER_DATA = Path(os.environ.get("FT_USER_DATA", Path(__file__).resolve().parents[1] / "user_data"))
REGISTER = [
    "INSERT INTO trials (strategy, exchange, variant) SELECT DISTINCT strategy, exchange, 'as-is' "
    "FROM strategy_windows ON CONFLICT DO NOTHING",
    "INSERT INTO trials (strategy, exchange, variant) SELECT DISTINCT strategy, exchange, md5(params::text) "
    "FROM walkforward WHERE params IS NOT NULL ON CONFLICT DO NOTHING",
    "INSERT INTO trials (strategy, exchange, variant) SELECT name, '', 'load-failed' "
    "FROM strategies WHERE load_error IS NOT NULL ON CONFLICT DO NOTHING",
]
LIVE = """
SELECT b.strategy, b.exchange, min(t.open_date), count(*) FILTER (WHERE NOT t.is_open),
       count(*) FILTER (WHERE NOT t.is_open AND t.exit_reason ILIKE '%stop%')
FROM bots b JOIN bot_trades t ON t.bot = b.name GROUP BY 1, 2
"""


PAIRS = """
SELECT c.strategy, c.exchange, count(*), sum(t.profit_ratio)
FROM backtest_trades t JOIN backtest_chunks c ON c.id = t.chunk_id WHERE t.close_date < %s
GROUP BY c.strategy, c.exchange, t.pair
"""


def venue_fee(exchange: str) -> float | None:
    path = USER_DATA / "exchanges" / f"{exchange}.json"
    return json.loads(path.read_text()).get("fee") if path.is_file() else None


def register(conn) -> int:
    for sql in REGISTER:
        conn.execute(sql)
    return conn.execute("SELECT count(*) FROM trials").fetchone()[0]


def load(conn, cfg: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    facts = suggest.load_facts(conn, cfg, now)
    daily: dict = {}
    lockbox = cfg["lockbox"]["start"]
    for s, e, day, ret in conn.execute("SELECT strategy, exchange, day, ret FROM strategy_daily WHERE day < %s "
                                       "ORDER BY 1, 2, 3", (lockbox,)):
        daily.setdefault((s, e), []).append((day, ret))
    facts["daily"] = {k: pd.Series(dict(v), dtype=float) for k, v in daily.items()}
    close = dict(conn.execute("SELECT day, btc_close FROM regimes ORDER BY day").fetchall())
    facts["close"] = pd.Series(close, dtype=float)
    facts["drift"] = {}
    for s, e, gap, n in conn.execute("SELECT strategy, exchange, gap, gap_trades FROM drift WHERE gap IS NOT NULL "
                                     "ORDER BY gap_trades"):
        facts["drift"][(s, e)] = {"gap": gap, "trades": n}  # most trades last, so that bot's gap stands
    facts["live"] = {(s, e): {"since": since, "closed": closed, "stopouts": stops}
                     for s, e, since, closed, stops in conn.execute(LIVE)}
    facts["pairs"] = {}
    for s, e, n, total in conn.execute(PAIRS, (lockbox,)):
        facts["pairs"].setdefault((s, e), []).append((n, total))
    facts["now"] = now
    return facts


def evidence(walkforward: dict, cfg: dict) -> dict:
    """Each pair's pooled tuned returns, in fold order, with their interval and each month's expectancy."""
    out = {}
    for (s, e), folds in walkforward.items():
        x = np.concatenate([np.asarray(f["tuned_returns"] or [], dtype=float) for f in folds])
        lo, hi = metrics.block_mean_ci(x, metrics.seed_of(s, e, "confidence"), level=cfg["expectancy_level"])
        out[(s, e)] = {"returns": x, "trades": len(x), "mean": float(x.mean()) if len(x) else None, "lo": lo, "hi": hi,
                       "months": [f["tuned_expectancy"] for f in folds if f["tuned_trades"]]}
    return out


def scores(ev: dict, cfg: dict) -> dict:
    """100 x P(true expectancy > 0) for every pair with enough trades, once there are enough such pairs."""
    members = [p for p, e in ev.items() if e["trades"] >= cfg["min_member_trades"] and e["lo"] is not None
               and e["hi"] > e["lo"]]
    if len(members) < cfg["min_population"]:
        return {}
    z = norm.ppf((1 + cfg["expectancy_level"]) / 2)
    mean, sd = metrics.shrink(np.array([ev[p]["mean"] for p in members]),
                              np.array([(ev[p]["hi"] - ev[p]["lo"]) / (2 * z) for p in members]))
    return {p: float(100 * norm.cdf(m / s)) for p, m, s in zip(members, mean, sd)}


def deflated(daily: pd.Series | None, w: dict, trials: int) -> tuple[float | None, float | None]:
    """The window's deflated Sharpe with the register's trial count, and that count."""
    if daily is None or not w.get("start_day") or not w.get("trials") or not w.get("trials_raw"):
        return None, None
    r = daily[(daily.index >= w["start_day"]) & (daily.index <= w["end_day"])]
    sd = r.std(ddof=1)
    if len(r) < 3 or not sd:
        return None, None
    n = max(trials, w["trials_raw"]) * w["trials"] / w["trials_raw"]
    return metrics.deflated_sharpe(float(r.mean() / sd), len(r), float(r.skew()), float(r.kurt()) + 3.0, n), n


def regime_spread(daily: pd.Series | None, close: pd.Series, cfg: dict) -> tuple[str | None, bool]:
    if daily is None or close.empty:
        return None, False
    bucket = metrics.trend_buckets(close, cfg["trend_band"]).reindex(daily.index)
    by = daily.groupby(bucket).agg(["sum", "count"])
    by = by[by["count"] >= cfg["min_regime_days"]]["sum"]
    gain = by[by > 0].sum()
    ok = (len(by) == 3 and int((by < 0).sum()) <= cfg["max_negative_regimes"]
          and gain > 0 and by.max() / gain <= cfg["max_regime_share"])
    return ", ".join(f"{k} {v:+.0%}" for k, v in by.items()) or None, bool(ok)


def live_gate(live: dict | None, close: pd.Series, now: dt.datetime, c: dict) -> dict:
    need = (f">= {c['min_live_days']} days, {c['min_live_trades']} trades, BTC down {c['min_live_btc_drop']:.0%}, "
            f"{c['min_live_stopouts']} stop-out")
    if not live:
        return gate("dry run", "no bot", need, False)
    days = (now - live["since"]).days
    seen = close[close.index >= live["since"].date()]
    drop = metrics.max_drawdown(seen.pct_change().dropna().to_numpy()) if len(seen) > 1 else 0.0
    return gate("dry run", f"{days} days, {live['closed']} trades, BTC down {drop:.0%}, {live['stopouts']} stop-outs", need,
                days >= c["min_live_days"] and live["closed"] >= c["min_live_trades"]
                and drop >= c["min_live_btc_drop"] and live["stopouts"] >= c["min_live_stopouts"])


def without_best_pair(pairs: list[tuple] | None) -> float | None:
    """Expectancy of every trade outside the strategy's most profitable pair, from (trades, summed return) per pair."""
    if not pairs or len(pairs) < 2:
        return None
    rest = sorted(pairs, key=lambda p: p[1])[:-1]
    return sum(p[1] for p in rest) / sum(p[0] for p in rest)


def venue_agreement(strategy: str, ev: dict, cfg: dict) -> tuple[int, int]:
    """On how many venues this strategy's out-of-sample expectancy is positive, of those with enough trades."""
    means = [v["mean"] for (s, _), v in ev.items() if s == strategy and v["trades"] >= cfg["min_member_trades"]]
    return sum(m > 0 for m in means), len(means)


def gates(pair: tuple, facts: dict, ev: dict | None, score: float | None, trials: int, cfg: dict,
          venues: tuple[int, int] = (0, 0)) -> list[dict]:
    """Every gate for one strategy and exchange, passed or not, in a fixed order."""
    c = cfg["confidence"]
    strategy, exchange = pair
    s = facts["strategies"].get(strategy) or {"lookahead": None, "recursive": None}
    series = facts["series"].get(pair) or {"windows": {}}
    w = series["windows"].get(c["window"]) or {}
    daily = facts["daily"].get(pair)
    ev = ev or {"returns": [], "trades": 0, "mean": None, "lo": None, "hi": None, "months": []}
    pbo = facts["overfit"].get((exchange, c["window"]))
    dsr, n = deflated(daily, w, trials)
    months = ev["months"]
    share = sum(m > 0 for m in months) / len(months) if months else None
    regime, regime_ok = regime_spread(daily, facts["close"], c)
    hold, worst, paid = w.get("hold_excess_lo"), w.get("worst_open"), w.get("fee_per_trade")
    fee = venue_fee(exchange)
    stressed = None if ev["mean"] is None or paid is None else (ev["mean"] + paid) * c["stress_gross_share"] - paid
    d = facts["drift"].get(pair)
    width = None if ev["lo"] is None else (ev["hi"] - ev["lo"]) / 2
    bias = [k for k in ("lookahead", "recursive") if s[k] is not False]
    stage = cfg["book"]["stages"][0]
    fall = metrics.reorder_drawdown(np.asarray(ev["returns"]) * stage["stake"] / stage["capital"],
                                    metrics.seed_of(strategy, exchange, "reorder")) if ev["trades"] else None
    rest = without_best_pair(facts["pairs"].get(pair))
    return [
        gate("bias", ("not clean: " + ", ".join(bias)) if bias else "clean", "lookahead and recursive clean", not bias),
        gate("costs", paid, f"within {c['fee_tolerance']:.0%} of 2 x {fee}",
             paid is not None and bool(fee) and abs(paid / (2 * fee) - 1) <= c["fee_tolerance"]),
        gate("out-of-sample trades", ev["trades"], f">= {c['min_oos_trades']}", ev["trades"] >= c["min_oos_trades"]),
        gate("expectancy interval low", ev["lo"], f"> 0 at {c['expectancy_level']:.0%}", ev["lo"] is not None and ev["lo"] > 0),
        gate(f"deflated Sharpe, {n:.0f} trials" if n else "deflated Sharpe", dsr, f">= {c['min_dsr']}",
             dsr is not None and dsr >= c["min_dsr"]),
        gate("overfit probability", pbo, f"<= {c['max_pbo']}", pbo is not None and pbo <= c["max_pbo"]),
        gate("months positive", None if share is None else f"{sum(m > 0 for m in months)} of {len(months)}",
             f">= {c['min_folds_positive']:.0%} of >= {c['min_folds']}",
             len(months) >= c["min_folds"] and share >= c["min_folds_positive"]),
        gate("regime spread", regime, f"3 buckets, <= {c['max_negative_regimes']} negative, "
             f"none over {c['max_regime_share']:.0%} of profit", regime_ok),
        gate("excess over hold, low", hold, f"> {c['hold_excess_lo_above']}",
             hold is not None and hold > c["hold_excess_lo_above"]),
        gate("worst open loss", worst, f">= -{c['max_open_loss']}", worst is not None and worst >= -c["max_open_loss"]),
        gate("cost stress", stressed, f"> 0 at {c['stress_gross_share']:.0%} of the gross edge",
             stressed is not None and stressed > 0),
        gate("slippage room", ev["lo"], f"> {c['slippage_per_trade']} a trade",
             ev["lo"] is not None and ev["lo"] > c["slippage_per_trade"]),
        gate("trades reordered, worst fall", fall, f"<= {cfg['book']['hard_stop']} at Stage 1 stake",
             fall is not None and fall <= cfg["book"]["hard_stop"]),
        gate("without its best pair", rest, "> 0", rest is not None and rest > 0),
        gate("venues agreeing", f"{venues[0]} of {venues[1]}", f">= {c['min_venues_agreeing']}",
             venues[0] >= c["min_venues_agreeing"]),
        gate("drift", None if d is None else d["gap"], f"inside the interval, >= {c['min_drift_trades']} trades",
             d is not None and width is not None and d["trades"] >= c["min_drift_trades"] and abs(d["gap"]) <= width),
        live_gate(facts["live"].get(pair), facts["close"], facts["now"], c),
        gate("score", score, f">= {c['ready_score']}", score is not None and score >= c["ready_score"]),
    ]


def rows(facts: dict, trials: int, cfg: dict) -> list[dict]:
    ev = evidence(facts["walkforward"], cfg["confidence"])
    score = scores(ev, cfg["confidence"])
    out = []
    for pair in sorted(set(facts["series"]) | set(ev)):
        g = gates(pair, facts, ev.get(pair), score.get(pair), trials, cfg, venue_agreement(pair[0], ev, cfg["confidence"]))
        e = ev.get(pair) or {}
        out.append({"strategy": pair[0], "exchange": pair[1], "score": score.get(pair), "ready": all(x["pass"] for x in g),
                    "passed": sum(x["pass"] for x in g), "oos_trades": e.get("trades", 0),
                    "oos_expectancy": e.get("mean"), "gates": Jsonb(g)})
    return out


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    trials = register(conn)
    out = rows(load(conn, cfg), trials, cfg)
    with conn.transaction():
        conn.execute("DELETE FROM confidence")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, oos_expectancy, "
                            "gates) VALUES (%(strategy)s, %(exchange)s, %(score)s, %(ready)s, %(passed)s, %(oos_trades)s, "
                            "%(oos_expectancy)s, %(gates)s)", out)
    return {"confidence": len(out), "confidence_ready": sum(r["ready"] for r in out), "trial_register": trials}


if __name__ == "__main__":
    with connect(autocommit=True) as c:
        print(f"confidence: {run(c)}", flush=True)
    sys.exit(0)
