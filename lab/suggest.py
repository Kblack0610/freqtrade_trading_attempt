"""Rule-based suggestions from the analysis tables, rebuilt after every analysis run.

promote  a strategy and exchange that clears every gate in suggest.toml [promote] and is not in the league there
watch    a lookahead-clean candidate that misses at most [watch] max_failed_gates of those gates
demote   a league bot whose own dry run loses with enough trades, whose backtests lose confidently, or whose
         strategy has lookahead bias
retire   a strategy with lookahead bias or one that fails to load
fix      a strategy whose recursive check failed or could not run for want of a startup_candle_count
retune   a strategy whose walk-forward hyperopt parameters beat it as it is on months hyperopt never saw

Suggestions are advice for a human. Nothing here edits the roster; the action text says what to change.
Run alone with `python lab/suggest.py`; lab/analyze.py runs it after it derives.
"""

import datetime as dt
import json
import math
import os
import sys
import tomllib
from pathlib import Path

from psycopg.types.json import Jsonb

import metrics
from db import connect

CONFIG = Path(os.environ.get("SUGGEST_CONFIG", Path(__file__).resolve().parent / "suggest.toml"))
ROSTER_STALE = dt.timedelta(days=1)  # a bot unseen this long no longer counts toward the league


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text())


def gate(name: str, value, need: str, ok: bool) -> dict:
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    return {"gate": name, "value": value, "need": need, "pass": bool(ok)}


def edge_on(series: dict, p: dict) -> bool:
    w = series["windows"].get(p["window"]) or {}
    lo = w.get("expectancy_lo")
    return (w.get("trades") or 0) >= p["min_trades"] and lo is not None and lo > p["expectancy_lo_above"]


def promote_gates(s: dict, series: dict, cfg: dict, venues: list[str] | None = None, pbo: float | None = None) -> list[dict]:
    """Every [promote] gate for one strategy and exchange, passed or not, in a fixed order. venues are the
    exchanges where the strategy clears the trades and expectancy gates; pbo is this exchange's overfit probability."""
    p = cfg["promote"]
    venues = venues or []
    w = series["windows"].get(p["window"]) or {}
    look = s["lookahead"]
    trades = w.get("trades") or 0
    full = [y for y in series["years"] if y["days"] >= p["min_year_days"]]
    share = sum(y["ret"] > 0 for y in full) / len(full) if full else None
    rho, other = series.get("max_rho") or (None, None)
    exp_lo, sh_lo, dsr, mdd = (w.get(k) for k in ("expectancy_lo", "sharpe_lo", "dsr", "max_drawdown"))
    return [
        gate("lookahead clean", "clean" if look is False else ("biased" if look else "not checked"), "clean",
             look is False),
        gate(f"trades in {p['window']}", trades, f">= {p['min_trades']}", trades >= p["min_trades"]),
        gate("expectancy interval low", exp_lo, f"> {p['expectancy_lo_above']}",
             exp_lo is not None and exp_lo > p["expectancy_lo_above"]),
        gate("Sharpe interval low", sh_lo, f"> {p['sharpe_lo_above']}",
             sh_lo is not None and sh_lo > p["sharpe_lo_above"]),
        gate("deflated Sharpe", dsr, f">= {p['min_dsr']}", dsr is not None and dsr >= p["min_dsr"]),
        gate("profitable years", None if share is None else f"{sum(y['ret'] > 0 for y in full)} of {len(full)}",
             f">= {p['min_years_profitable']:.0%} of >= {p['min_years']}",
             len(full) >= p["min_years"] and share is not None and share >= p["min_years_profitable"]),
        gate("max drawdown", mdd, f"<= {p['max_drawdown']}", mdd is not None and mdd <= p["max_drawdown"]),
        gate("correlation with league" + (f" ({other})" if other else ""), rho, f"|rho| <= {p['max_rho']}",
             rho is None or abs(rho) <= p["max_rho"]),
        gate("venues with an edge", ", ".join(venues) or "none", f">= {p['min_venues']}",
             len(venues) >= p["min_venues"]),
        gate(f"venue overfit probability, {p['window']}", pbo, f"<= {p['max_pbo']}", pbo is None or pbo <= p["max_pbo"]),
    ]


def roster_line(strategy: str, exchange: str) -> str:
    return f"{exchange}-{strategy.lower()}".ljust(21) + exchange.ljust(11) + strategy


def decide(facts: dict, cfg: dict) -> list[dict]:
    """Pure: the suggestion rows for one snapshot of facts. See load_facts for the shape."""
    out = []
    strategies, series, bots = facts["strategies"], facts["series"], facts["bots"]
    league = {(b["strategy"], b["exchange"]) for b in bots}
    replicas = len(bots)

    for name, s in sorted(strategies.items()):
        if s["load_error"]:
            out.append(dict(kind="retire", strategy=name, exchange="", score=None,
                            summary=f"The strategy fails to load: {s['load_error']}",
                            action=f"Fix or delete {s['file']}: every backtest and bias check skips it until it loads.",
                            gates=[gate("loads", s["load_error"], "loads", False)]))
            continue
        if s["lookahead"] is True:
            out.append(dict(kind="retire", strategy=name, exchange="", score=None,
                            summary=f"Every backtest of it is void: {s['lookahead_indicators'] or 'its signals'} read candles "
                                    "that had not closed yet (lookahead bias).",
                            action=f"Delete {s['file']}, or rewrite those indicators so each candle uses only "
                                   "earlier ones, then let the next run recheck it.",
                            gates=[gate("lookahead clean", "biased", "clean", False)]))
            continue  # retiring it supersedes fixing it
        if s["recursive"] is True:
            out.append(dict(kind="fix", strategy=name, exchange="", score=None,
                            summary=f"Live values can differ from the backtest: {s['recursive_indicators'] or 'an indicator'} "
                                    f"still moves after the {s['startup']} startup candles the strategy declares.",
                            action=f"Raise startup_candle_count in {s['file']} until the recursive check is clean; "
                                   "if the indicator is only plotted, this changes nothing.",
                            gates=[gate("indicators stable after startup", "unstable", "stable", False)]))
        elif s["recursive"] is None and not s["startup"]:
            out.append(dict(kind="fix", strategy=name, exchange="", score=None,
                            summary="The strategy declares no startup candle count, so the recursive check cannot run and its "
                                    "first candles feed indicators that have not warmed up.",
                            action=f"Set startup_candle_count in {s['file']} to the longest indicator period it uses.",
                            gates=[gate("startup candles declared", s["startup"], "> 0", False)]))

    candidates = []
    for (name, exchange), ser in sorted(series.items()):
        s = strategies.get(name)
        if not s or s["load_error"] or (name, exchange) in league:
            continue
        venues = sorted(e for (n, e), x in series.items() if n == name and edge_on(x, cfg["promote"]))
        gates = promote_gates(s, ser, cfg, venues, facts.get("overfit", {}).get((exchange, cfg["promote"]["window"])))
        failed = [g for g in gates if not g["pass"]]
        w = ser["windows"].get(cfg["promote"]["window"]) or {}
        score = w.get("sharpe_lo")
        if not failed:
            out.append(dict(kind="promote", strategy=name, exchange=exchange, score=score,
                            summary=f"Passes every promote gate on {exchange}: {w.get('trades')} trades in "
                                    f"{cfg['promote']['window']}, per-trade expectancy {w.get('expectancy_lo'):+.2%} at "
                                    f"the 90% low (needs above {cfg['promote']['expectancy_lo_above']:.2%}), deflated "
                                    f"Sharpe {w.get('dsr'):.2f} (needs {cfg['promote']['min_dsr']:.2f}).",
                            action=f"In home-config, append to apps/freqtrade/roster.tsv:\n{roster_line(name, exchange)}\n"
                                   f"then set replicas in statefulset.yaml to {replicas + 1}.",
                            gates=gates))
        elif s["lookahead"] is False and len(failed) <= cfg["watch"]["max_failed_gates"]:
            candidates.append(dict(kind="watch", strategy=name, exchange=exchange, score=score,
                                   summary="Held back from promotion by: " + ", ".join(g["gate"] for g in failed) + ".",
                                   action="Nothing to do yet; it moves to Promote when a later run clears the rest.",
                                   gates=gates))
    rank = sorted(candidates, key=lambda r: -(r["score"] if r["score"] is not None else -math.inf))
    out += rank[:cfg["watch"]["limit"]]

    d = cfg["demote"]
    research = {e for (_, e) in series}
    for b in sorted(bots, key=lambda b: b["name"]):
        s = strategies.get(b["strategy"]) or {}
        gates, reasons = [], []
        live_ok = b["live_trades"] >= d["min_live_trades"]
        gates.append(gate(f"dry run, {d['live_days']}d profit", b["live_pnl"],
                          f">= 0 over >= {d['min_live_trades']} trades",
                          not live_ok or b["live_pnl"] >= 0))
        if live_ok and b["live_pnl"] < 0:
            reasons.append(f"the dry run lost {-b['live_pnl']:.2f} over {b['live_trades']} trades in {d['live_days']} days")
        on = [b["exchange"]] if b["exchange"] in research else sorted(research)
        bt = [series[(b["strategy"], e)]["windows"].get(d["window"]) for e in on if (b["strategy"], e) in series]
        bt = [w for w in bt if w and (w.get("trades") or 0) >= d["min_trades"]]
        losing = bool(bt) and all(w.get("expectancy_hi") is not None and w["expectancy_hi"] < 0 for w in bt)
        gates.append(gate(f"backtest {d['window']} expectancy interval high",
                          max((w["expectancy_hi"] for w in bt if w.get("expectancy_hi") is not None), default=None),
                          ">= 0", not losing))
        if losing:
            where = b["exchange"] if b["exchange"] in research else "every research exchange"
            reasons.append(f"the {d['window']} backtests on {where} lose money even at the 90% high")
        if s.get("lookahead") is True:
            gates.append(gate("lookahead clean", "biased", "clean", False))
            reasons.append("the strategy has lookahead bias")
        if reasons:
            why = "; ".join(reasons)
            out.append(dict(kind="demote", strategy=b["strategy"], exchange=b["exchange"], bot=b["name"], score=None,
                            summary=why[0].upper() + why[1:] + ".",
                            action=f"Replace the {b['name']} row of roster.tsv in place with a promoted candidate and "
                                   "delete that pod's state-freqtrade-league-<N> PVC. Deleting the row instead moves "
                                   "every later bot onto another bot's state.",
                            gates=gates))
    out += retune_rows(facts, cfg)
    for r in out:
        r.setdefault("bot", "")
    return out


def retune_rows(facts: dict, cfg: dict) -> list[dict]:
    c, out = cfg["retune"], []
    for (name, exchange), folds in sorted(facts.get("walkforward", {}).items()):
        s = facts["strategies"].get(name)
        if not s or s["load_error"] or s["lookahead"] is not False:
            continue
        newest = folds[-c["folds"]:]
        beat = [f for f in newest if f["params"] and (f["tuned_trades"] or 0) >= c["min_fold_trades"]
                and f["tuned_profit"] is not None and f["base_profit"] is not None
                and f["tuned_profit"] > f["base_profit"]]
        tuned = [x for f in folds if f["params"] for x in (f["tuned_returns"] or [])]
        base = [x for f in folds if f["params"] for x in (f["base_returns"] or [])]
        t_mean = sum(tuned) / len(tuned) if tuned else None
        b_mean = sum(base) / len(base) if base else None
        t_lo, _ = metrics.mean_ci(tuned, metrics.seed_of(name, exchange, "retune"))
        g_lo, _ = metrics.diff_ci(tuned, base, metrics.seed_of(name, exchange, "retune gain"))
        gates = [
            gate(f"tuned beats as is, newest {c['folds']} months", f"{len(beat)} of {len(newest)}",
                 f"{c['folds']} of {c['folds']}, >= {c['min_fold_trades']} trades each", len(beat) == c["folds"]),
            gate("gain per trade over as is, interval low, all months", g_lo, f"> {c['min_gain']}",
                 t_mean is not None and (not base or (g_lo is not None and g_lo > c["min_gain"]))),
            gate("tuned trades, all months", len(tuned), f">= {c['min_trades']}", len(tuned) >= c["min_trades"]),
            gate("tuned expectancy interval low, all months", t_lo, f"> {c['min_oos_expectancy']}",
                 t_lo is not None and t_lo > c["min_oos_expectancy"]),
        ]
        if not all(g["pass"] for g in gates):
            continue
        last = newest[-1]
        params = {"strategy_name": name, "params": last["params"], "ft_stratparam_v": 1}
        target = str(Path(s["file"]).with_suffix(".json"))
        out.append(dict(kind="retune", strategy=name, exchange=exchange, score=t_mean - (b_mean or 0.0),
                        summary=f"Tuned parameters beat the strategy as is on {exchange} in each of the newest {c['folds']} "
                                f"unseen months; over all {len(folds)} months, {t_mean:+.2%} per trade tuned against "
                                f"{(b_mean or 0.0):+.2%} as is ({len(tuned)} trades).",
                        action=f"Save the newest month's tuned parameters (trained {last['train_start']} to "
                               f"{last['fold_start']}) as {target}:\n{json.dumps(params, indent=2)}",
                        gates=gates))
    return out


def load_facts(conn, cfg: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    strategies = {}
    for name, file, startup, err in conn.execute(
            "SELECT name, file, startup_candle_count, load_error FROM strategies"):
        strategies[name] = {"file": file, "startup": startup, "load_error": err, "lookahead": None,
                            "lookahead_indicators": None, "recursive": None, "recursive_indicators": None}
    for name, kind, has_bias, ind in conn.execute(
            "SELECT strategy, kind, has_bias, biased_indicators FROM bias_checks"):
        if name in strategies:
            strategies[name][kind] = has_bias
            strategies[name][f"{kind}_indicators"] = ind

    series: dict = {}
    cur = conn.cursor()
    cur.execute("SELECT * FROM strategy_windows")
    cols = [c.name for c in cur.description]
    for row in cur:
        r = dict(zip(cols, row))
        series.setdefault((r["strategy"], r["exchange"]), {"windows": {}, "years": [], "max_rho": None})
        series[(r["strategy"], r["exchange"])]["windows"][r["win"]] = r
    for s, e, year, days, ret in conn.execute(
            "SELECT strategy, exchange, extract(year FROM day)::int, count(*), "
            "exp(sum(ln(greatest(1 + ret, 1e-9)))) - 1 FROM strategy_daily GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"):
        if (s, e) in series:
            series[(s, e)]["years"].append({"year": year, "days": days, "ret": ret})
    for s, e, other, rho in conn.execute(
            "SELECT DISTINCT ON (strategy, exchange) strategy, exchange, other, rho FROM correlations "
            "ORDER BY strategy, exchange, abs(rho) DESC"):
        if (s, e) in series:
            series[(s, e)]["max_rho"] = (rho, other)

    since = now - dt.timedelta(days=cfg["demote"]["live_days"])
    bots = [{"name": n, "exchange": e, "strategy": s, "live_trades": t, "live_pnl": p} for n, e, s, t, p in conn.execute(
        "SELECT b.name, b.exchange, b.strategy, count(t.trade_id), coalesce(sum(t.profit_abs), 0) FROM bots b "
        "LEFT JOIN bot_trades t ON t.bot = b.name AND NOT t.is_open AND t.close_date >= %s "
        "WHERE b.last_seen >= %s GROUP BY 1, 2, 3", (since, now - ROSTER_STALE))]
    walkforward: dict = {}
    cur.execute("SELECT * FROM walkforward WHERE ok AND test_end <= %s ORDER BY strategy, exchange, fold_start",
                (cfg["lockbox"]["start"],))
    cols = [c.name for c in cur.description]
    for row in cur:
        r = dict(zip(cols, row))
        walkforward.setdefault((r["strategy"], r["exchange"]), []).append(r)
    overfit = {(e, w): pbo for e, w, pbo in conn.execute("SELECT exchange, win, pbo FROM overfit")}
    return {"strategies": strategies, "series": series, "bots": bots, "walkforward": walkforward, "overfit": overfit}


def store(conn, rows: list[dict]) -> dict:
    """Upsert this run's suggestions, keeping first_seen, and delete the ones that no longer apply."""
    with conn.transaction():
        started = conn.execute("SELECT now()").fetchone()[0]
        for r in rows:
            conn.execute(
                "INSERT INTO suggestions (kind, strategy, exchange, bot, score, summary, action, gates) "
                "VALUES (%(kind)s, %(strategy)s, %(exchange)s, %(bot)s, %(score)s, %(summary)s, %(action)s, %(gates)s) "
                "ON CONFLICT (kind, strategy, exchange, bot) DO UPDATE SET score = EXCLUDED.score, "
                "summary = EXCLUDED.summary, action = EXCLUDED.action, gates = EXCLUDED.gates, last_seen = now()",
                {**r, "score": r["score"] if r["score"] is None or math.isfinite(r["score"]) else None,
                 "gates": Jsonb(r["gates"])})
        gone = conn.execute("DELETE FROM suggestions WHERE last_seen < %s", (started,)).rowcount
    counts: dict = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return {"suggestions": counts, "suggestions_gone": gone}


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    return store(conn, decide(load_facts(conn, cfg), cfg))


if __name__ == "__main__":
    with connect(autocommit=True) as c:
        print(f"suggest: {run(c)}", flush=True)
    sys.exit(0)
