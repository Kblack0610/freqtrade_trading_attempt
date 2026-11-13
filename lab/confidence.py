#!/usr/bin/env python3
"""Whether a strategy on an exchange has earned real money. Advice for a human, like lab/suggest.py: nothing
here touches a bot, and lab/league.py refuses real money whatever this says.

The score is 100 x P(true net expectancy per trade > 0): each strategy's pooled walk-forward result, shrunk
toward the population of everything walk-forward has tried (metrics.shrink). It is one number beside hard gates,
not a blend of them, because no weighting of these gates has any evidence behind it. Ready means every gate in
suggest.toml [confidence] passes, the score included. It means worth a small capped experiment, not safe.

Out of sample means the tuned returns: months hyperopt never saw. So the score is about tuning on the months
before and trading the next, and the walk-forward candidates were themselves chosen on a trailing year.

Retiring is its own test, apart from lab/watcher.py's hard stop on capital: decay() runs a one-sided CUSUM (Page 1954;
a sequential probability ratio test restarted at zero) over a bot's closed dry-run trades, against the low end of
the strategy's out-of-sample interval. A crossing is the "not decayed" gate failing and a retire suggestion for each
bot running it. One bad week cannot cross the bar; a sustained run of trades below the out-of-sample edge does.

Every run first adds what was tried to the trial register, `trials`, which keeps a row when its strategy goes:
each strategy x exchange as it stands, under each setup overlay (lab/variants.toml) and with each tuned parameter set.
The effective trial count is the crypto lab's share of the register (trial_count) at the window's effective share
(metrics.effective_trials).

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

from lab import metrics, robustness, suggest
from lab.db import connect
from lab.markets import venues as market_venues
from lab.suggest import gate

USER_DATA = Path(os.environ.get("FT_USER_DATA", Path(__file__).resolve().parents[1] / "user_data"))
REGISTER = [
    "INSERT INTO trials (strategy, exchange, variant) SELECT DISTINCT strategy, exchange, 'as-is' "
    "FROM strategy_windows ON CONFLICT DO NOTHING",
    "INSERT INTO trials (strategy, exchange, variant) SELECT DISTINCT strategy, exchange, 'overlay:' || overlay "
    "FROM overlay_windows ON CONFLICT DO NOTHING",
    "INSERT INTO trials (strategy, exchange, variant) SELECT DISTINCT strategy, exchange, md5(params::text) "
    "FROM walkforward WHERE params IS NOT NULL ON CONFLICT DO NOTHING",
    "INSERT INTO trials (strategy, exchange, variant) SELECT w.strategy, w.exchange, "
    "'robustness:' || md5(jsonb_build_array(w.params, w.robustness->'_settings', r.key)::text) "
    "FROM walkforward w CROSS JOIN LATERAL jsonb_each(coalesce(w.robustness, '{}'::jsonb)) r "
    "WHERE r.key <> '_settings' ON CONFLICT DO NOTHING",
    "INSERT INTO trials (strategy, exchange, variant) SELECT name, '', 'load-failed' "
    "FROM strategies WHERE load_error IS NOT NULL ON CONFLICT DO NOTHING",
]
LIVE = """
SELECT b.strategy, b.exchange, min(t.open_date), count(*) FILTER (WHERE NOT t.is_open),
       count(*) FILTER (WHERE NOT t.is_open AND t.exit_reason ILIKE '%stop%')
FROM bots b JOIN bot_trades t ON t.bot = b.name GROUP BY 1, 2
"""


LIVE_RETURNS = """
SELECT b.strategy, b.exchange, t.profit_ratio FROM bots b JOIN bot_trades t ON t.bot = b.name
WHERE NOT t.is_open AND t.profit_ratio IS NOT NULL ORDER BY t.close_date, t.trade_id
"""
ROSTER = "SELECT strategy, exchange, name FROM bots WHERE last_seen >= %s ORDER BY name"

PAIRS = """
SELECT t.strategy, t.exchange, count(*), sum(t.profit_ratio)
FROM walkforward_trades t JOIN walkforward w USING (strategy, exchange, fold_start)
WHERE t.variant = 'tuned' AND w.ok AND w.test_end <= %s AND t.close_date < %s
GROUP BY t.strategy, t.exchange, t.pair
"""


def venue_fee(exchange: str) -> float | None:
    path = USER_DATA / "exchanges" / f"{exchange}.json"
    return json.loads(path.read_text()).get("fee") if path.is_file() else None


def trial_count(conn) -> int:
    """The crypto lab's tries. lab/markets/backtest.py registers event-contract variants in the same register under
    their venue and counts only those; they are another lane's tries and do not raise this lane's bar."""
    return conn.execute("SELECT count(*) FROM trials WHERE NOT (exchange = ANY(%s))", (market_venues(),)).fetchone()[0]


def register(conn) -> int:
    for sql in REGISTER:
        conn.execute(sql)
    return trial_count(conn)


def load(conn, cfg: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    facts = suggest.load_facts(conn, cfg, now)
    daily: dict = {}
    lockbox = cfg["lockbox"]["start"]
    for s, e, day, ret in conn.execute("SELECT strategy, exchange, day, ret FROM strategy_daily WHERE day < %s "
                                       "ORDER BY 1, 2, 3", (lockbox,)):
        daily.setdefault((s, e), []).append((day, ret))
    facts["daily"] = {k: pd.Series(dict(v), dtype=float) for k, v in daily.items()}
    reg = pd.DataFrame(conn.execute("SELECT day, btc_close, trend FROM regimes ORDER BY day").fetchall(),
                       columns=["day", "btc_close", "trend"]).set_index("day")
    facts["close"], facts["trend"] = reg["btc_close"].astype(float), reg["trend"]
    oos: dict = {}
    for s, e, day, ret in conn.execute("SELECT strategy, exchange, day, ret FROM walkforward_daily WHERE variant = 'tuned' "
                                       "AND day < %s ORDER BY 1, 2, 3", (lockbox,)):
        oos.setdefault((s, e), {})[pd.Timestamp(day)] = ret
    facts["oos"] = {k: pd.Series(v, dtype=float) for k, v in oos.items()}
    facts["drift"] = {}
    for s, e, gap, n, gaps in conn.execute("SELECT strategy, exchange, gap, gap_trades, gaps FROM drift "
                                           "WHERE gap IS NOT NULL ORDER BY gap_trades"):
        facts["drift"][(s, e)] = {"gap": gap, "trades": n, "gaps": gaps or []}  # most trades last, so that bot's gap stands
    facts["live"] = {(s, e): {"since": since, "closed": closed, "stopouts": stops}
                     for s, e, since, closed, stops in conn.execute(LIVE)}
    facts["live_returns"] = {}
    for s, e, ret in conn.execute(LIVE_RETURNS):
        facts["live_returns"].setdefault((s, e), []).append(ret)
    facts["roster"] = {}
    for s, e, name in conn.execute(ROSTER, (now - suggest.ROSTER_STALE,)):
        facts["roster"].setdefault((s, e), []).append(name)
    facts["pairs"] = {}
    for s, e, n, total in conn.execute(PAIRS, (lockbox, lockbox)):
        facts["pairs"].setdefault((s, e), []).append((n, total))
    facts["now"] = now
    return facts


def interval(returns, seed: int, cfg: dict) -> dict:
    """Per-trade net returns in time order with the mean's block-bootstrap interval: what scores() reads. Its callers
    are walk-forward folds here and event-contract backtests (lab/markets/backtest.py)."""
    x = np.asarray(returns, dtype=float)
    lo, hi = metrics.block_mean_ci(x, seed, level=cfg["expectancy_level"])
    return {"returns": x, "trades": len(x), "mean": float(x.mean()) if len(x) else None, "lo": lo, "hi": hi}


def evidence(walkforward: dict, cfg: dict) -> dict:
    """Each pair's pooled tuned returns, in fold order, with their interval and each month's expectancy."""
    return {(s, e): {**interval(np.concatenate([np.asarray(f["tuned_returns"] or [], dtype=float) for f in folds]),
                                metrics.seed_of(s, e, "confidence"), cfg),
                     "months": [f["tuned_expectancy"] for f in folds if f["tuned_trades"]]}
            for (s, e), folds in walkforward.items()}


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


def backtest_length(folds: list[dict], oos: pd.Series | None, trials: int, c: dict) -> tuple[float, float | None, bool]:
    """The tuned out-of-sample years (calendar days of the test months, zero where nothing closed), the years
    metrics.min_backtest_years asks for at the register's size, and whether the first reaches min_btl_ratio times
    the second."""
    days = [d for f in folds if f.get("params") for d in pd.date_range(f["fold_start"], f["test_end"], inclusive="left")]
    years = len(days) / metrics.YEAR
    r = (oos if oos is not None else pd.Series(dtype=float)).reindex(pd.DatetimeIndex(days)).fillna(0.0)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    sharpe = float(r.mean() / sd * np.sqrt(metrics.YEAR)) if sd and np.isfinite(sd) else None
    need = metrics.min_backtest_years(sharpe, trials)
    return years, need, need is not None and years >= c["min_btl_ratio"] * need


def regime_spread(daily: pd.Series | None, trend: pd.Series, cfg: dict) -> tuple[str | None, bool]:
    if daily is None or trend.empty:
        return None, False
    bucket = trend.reindex(daily.index)
    by = daily.groupby(bucket).agg(["sum", "count"])
    by = by[by["count"] >= cfg["min_regime_days"]]["sum"]
    gain = by[by > 0].sum()
    ok = (len(by) == 3 and int((by < 0).sum()) <= cfg["max_negative_regimes"]
          and gain > 0 and by.max() / gain <= cfg["max_regime_share"])
    return ", ".join(f"{k} {v:+.0%}" for k, v in by.items()) or None, bool(ok)


def league_alpha(facts: dict, c: dict) -> float:
    """The anytime-valid level for one sequence: alpha split by Bonferroni over the paper bots live now, so the
    league-wide false pass rate stays at alpha however often the nightly job reads them."""
    return c["anytime_alpha"] / max(1, len(facts["live"]))


def expectancy_bound(returns, alpha: float, c: dict) -> tuple[float | None, float | None]:
    """The anytime-valid interval of the mean paper return per closed trade, as of the last one."""
    if not len(returns):
        return None, None
    lo, hi = metrics.confidence_sequence(returns, *c["live_return_bounds"], alpha)
    return float(lo[-1]), float(hi[-1])


def live_gate(live: dict | None, returns, close: pd.Series, now: dt.datetime, c: dict, alpha: float) -> dict:
    need = (f"paper expectancy sequence low > 0 at alpha {alpha:.4f}; >= {c['min_live_days']} days, "
            f"BTC down {c['min_live_btc_drop']:.0%}, {c['min_live_stopouts']} stop-out")
    if not live:
        return gate("dry run", "no bot", need, False)
    days = (now - live["since"]).days
    seen = close[close.index >= live["since"].date()]
    drop = metrics.max_drawdown(seen.pct_change().dropna().to_numpy()) if len(seen) > 1 else 0.0
    lo, hi = expectancy_bound(returns, alpha, c)
    seq = "no trades" if lo is None else f"expectancy {lo:+.4f} to {hi:+.4f}" + (" (failed early)" if hi < 0 else "")
    return gate("dry run", f"{days} days, {live['closed']} trades, {seq}, BTC down {drop:.0%}, {live['stopouts']} stop-outs",
                need, lo is not None and lo > 0 and days >= c["min_live_days"]
                and drop >= c["min_live_btc_drop"] and live["stopouts"] >= c["min_live_stopouts"])


def drift_gate(d: dict | None, width: float | None, alpha: float, c: dict) -> dict:
    """The paper-minus-backtest gap sequence sits inside the expectancy interval's half width: an equivalence test."""
    need = f"gap sequence inside +-the expectancy interval half width at alpha {alpha:.4f}"
    if d is None or width is None or not d.get("gaps"):
        return gate("drift", None if d is None else d["gap"], need, False)
    b = c["drift_bound"]
    lo, hi = metrics.confidence_sequence(d["gaps"], -b, b, alpha)
    lo, hi = float(lo[-1]), float(hi[-1])
    note = " (failed early)" if lo > width or hi < -width else ""
    return gate("drift", f"{d['gap']:+.4f}, sequence {lo:+.4f} to {hi:+.4f} over {len(d['gaps'])} trades{note}", need,
                lo >= -width and hi <= width)


def decay(live, ev: dict, c: dict) -> dict | None:
    """CUSUM of the log-likelihood ratio, no edge against the out-of-sample low end, over live trades in close order.

    Normal trades with the pooled out-of-sample standard deviation: H0 mean is the interval's low end (the worst edge
    the walk-forward still supports), H1 mean is zero. The statistic floors at zero, so old good trades bank nothing.
    The bar is ln(decay_false_alarm_trades), Lorden's bound: if the edge is at least the low end, one false alarm
    takes that many trades on average. None when there is no edge to decay from or no live trade."""
    x = np.asarray(live, dtype=float)
    r = np.asarray(ev["returns"], dtype=float)
    if not len(x) or ev["lo"] is None or ev["lo"] <= 0 or len(r) < c["min_member_trades"] or not r.std(ddof=1):
        return None
    mu0, var = ev["lo"], r.var(ddof=1)
    step = (mu0 / var) * (mu0 / 2 - x)
    stat, peak, at = 0.0, 0.0, None
    bar = float(np.log(c["decay_false_alarm_trades"]))
    for i, v in enumerate(step, 1):
        stat = max(0.0, stat + v)
        peak = max(peak, stat)
        if at is None and stat >= bar:
            at = i
    return {"stat": stat, "peak": peak, "bar": bar, "trades": len(x), "at": at}


def decay_gate(d: dict | None, c: dict) -> dict:
    need = (f"CUSUM under ln({c['decay_false_alarm_trades']}) = {np.log(c['decay_false_alarm_trades']):.1f}: live trades "
            "not sustained below the out-of-sample low end")
    if d is None:
        return gate("not decayed", "nothing to test", need, True)
    value = (f"decayed at trade {d['at']} of {d['trades']}" if d["at"]
             else f"{d['stat']:.1f} of {d['bar']:.1f}, {d['trades']} trades")
    return gate("not decayed", value, need, d["at"] is None)


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


def slippage_gate(ev: dict, c: dict) -> dict:
    sweep = c["slippage_sweep"]
    values = [f"{cost:.2%}: {ev['lo'] - cost:+.4f}" for cost in sweep] if ev["lo"] is not None else []
    return gate("slippage sweep", ", ".join(values) or None, "interval low > 0 at every extra round-trip cost",
                bool(sweep) and ev["lo"] is not None and all(ev["lo"] > cost for cost in sweep))


def robustness_gates(folds: list[dict], pair: tuple, c: dict) -> list[dict]:
    pooled: dict = {}
    settings = robustness.settings(c)
    errors: dict = {kind: [] for kind in ("delay", "nudge", "detail")}
    for f in folds:
        expected = robustness.plan(c, f.get("params") or {})
        stored = f.get("robustness") or {}
        for name in expected:
            kind = name.split(":", 1)[0]
            r = stored.get(name) or {}
            if stored.get("_settings") != settings:
                r = {"error": "missing or stale experiment settings"}
            values = r.get("returns")
            if not r.get("ok") or values is None or not np.isfinite(values).all():
                errors[kind].append(f"{f['fold_start']}: {name}: {r.get('error') or 'missing or invalid evidence'}")
            else:
                pooled.setdefault(name, []).extend(values)
    out = []
    for kind, label in (("delay", "entry delay"), ("nudge", "parameter nudges"), ("detail", "timeframe detail")):
        tested = []
        ok = bool(folds) and not errors[kind]
        for name, values in pooled.items():
            if name.split(":", 1)[0] != kind:
                continue
            ev = interval(values, metrics.seed_of(*pair, name), c)
            passed = ev["trades"] >= c["min_member_trades"] and ev["lo"] is not None and ev["lo"] > 0
            ok = ok and passed
            tested.append(f"{name}: {ev['trades']} trades, low {ev['lo']}")
        out.append(gate(label, "; ".join(errors[kind] + tested) or "no evidence",
                        f"all scenarios, >= {c['min_member_trades']} trades each, interval low > 0", ok and bool(tested)))
    return out


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
    years, need_years, btl_ok = backtest_length(facts["walkforward"].get(pair, []), facts["oos"].get(pair), trials, c)
    regime, regime_ok = regime_spread(daily, facts["trend"], c)
    hold, worst, paid = w.get("hold_excess_lo"), w.get("worst_open"), w.get("fee_per_trade")
    fee = venue_fee(exchange)
    stressed = None if ev["mean"] is None or paid is None else (ev["mean"] + paid) * c["stress_gross_share"] - paid
    d = facts["drift"].get(pair)
    alpha = league_alpha(facts, c)
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
        gate("minimum backtest length", None if need_years is None else f"{years:.1f} y of {need_years:.1f} y",
             f"out-of-sample years >= {c['min_btl_ratio']:g} x 2 ln({trials}) / SR^2", btl_ok),
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
        slippage_gate(ev, c),
        *robustness_gates(facts["walkforward"].get(pair, []), pair, c),
        gate("trades reordered, worst fall", fall, f"<= {cfg['book']['hard_stop']} at Stage 1 stake",
             fall is not None and fall <= cfg["book"]["hard_stop"]),
        gate("without its best pair", rest, "> 0", rest is not None and rest > 0),
        gate("venues agreeing", f"{venues[0]} of {venues[1]}", f">= {c['min_venues_agreeing']}",
             venues[0] >= c["min_venues_agreeing"]),
        drift_gate(d, width, alpha, c),
        live_gate(facts["live"].get(pair), facts["live_returns"].get(pair, []), facts["close"], facts["now"], c, alpha),
        decay_gate(decay(facts["live_returns"].get(pair, []), ev, c), c),
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


def retire_rows(facts: dict, cfg: dict) -> list[dict]:
    """A retire suggestion for each roster bot whose strategy's live trades crossed the decay bar."""
    c, out = cfg["confidence"], []
    ev = evidence(facts["walkforward"], c)
    for pair, bots in sorted(facts["roster"].items()):
        d = decay(facts["live_returns"].get(pair, []), ev.get(pair) or {"returns": [], "lo": None}, c)
        if not d or d["at"] is None:
            continue
        g = decay_gate(d, c)
        for bot in bots:
            out.append(dict(kind="retire", strategy=pair[0], exchange=pair[1], bot=bot, score=None,
                            summary=f"Live trades have decayed: the CUSUM crossed its bar of {d['bar']:.1f} at trade {d['at']} "
                                    f"of {d['trades']}, so they sit below the out-of-sample low end of "
                                    f"{ev[pair]['lo']:+.2%} a trade for long enough that chance is unlikely.",
                            action=f"Retire {bot} once it is flat: replace its row of roster.tsv in place with a promoted "
                                   "candidate and delete that pod's state-freqtrade-league-<N> PVC. This is the decay test "
                                   "of docs/operating-rules.md; a drawdown alone is not.",
                            gates=[g]))
    return out


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    trials = register(conn)
    facts = load(conn, cfg)
    out = rows(facts, trials, cfg)
    with conn.transaction():
        conn.execute("DELETE FROM confidence")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, oos_expectancy, "
                            "gates) VALUES (%(strategy)s, %(exchange)s, %(score)s, %(ready)s, %(passed)s, %(oos_trades)s, "
                            "%(oos_expectancy)s, %(gates)s)", out)
    retired = suggest.store(conn, retire_rows(facts, cfg), suggest.DECAY)
    return {"confidence": len(out), "confidence_ready": sum(r["ready"] for r in out), "trial_register": trials,
            "decayed": sum(retired["suggestions"].values())}


if __name__ == "__main__":
    with connect(autocommit=True) as c:
        print(f"confidence: {run(c)}", flush=True)
    sys.exit(0)
