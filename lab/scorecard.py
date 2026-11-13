#!/usr/bin/env python3
"""The out-of-sample result per market climate: what each strategy, and BTC held, earned on the days of each climate.

A climate is the BTC trend bucket (up, down, flat) and the volatility tercile (both from lab/metrics.regimes) of
`regimes`, both read at the close of the day before, so a day's label never uses that day's return. A strategy's
days are the calendar days of its walk-forward test months, zero where it did not trade, from `walkforward_daily`
(trade profit ratios summed per close day; lab/walkforward.py writes them, trades across fold ends already purged).
A mixture's days are the months lab/mixtures.py scored it on, from `mixture_daily`.
annual is the mean daily value x 365; lo and hi are its block-bootstrap interval. A cell clears when lo is above zero.
The interval is per cell and not corrected for how many cells and strategies were looked at: `trials` is the
register's size, the bar that the deflated Sharpe on /confidence applies. Advice for a human; nothing here trades.

Run alone with `python -m lab.scorecard`; lab/analyze.py and lab/walkforward.py run it after the mixtures.
"""

import sys

import pandas as pd

from lab import confidence, metrics, suggest
from lab.db import connect

COLS = ["kind", "name", "exchange", "variant", "cell", "days", "trades", "annual", "lo", "hi", "trials"]
# Out-of-sample spans and days before the lockbox only, as confidence and the book read them; horizon shares both.
SPANS = ("SELECT strategy, exchange, min(fold_start), max(test_end) FROM walkforward WHERE ok AND test_end <= %s "
         "GROUP BY 1, 2")
DAILY = ("SELECT strategy, exchange, variant, day, ret, trades FROM walkforward_daily WHERE day < %s "
         "ORDER BY 1, 2, 3, 4")


def labels(reg: pd.DataFrame) -> pd.Series:
    """trend/vol per day from `regimes` (index: day), each read at the day before; a day without both has no label."""
    both = reg[["trend", "vol_bucket"]].astype(object).dropna()
    return (both["trend"] + "/" + both["vol_bucket"]).reindex(reg.index).shift(1).dropna()


def cells(daily: pd.Series, trades: pd.Series, label: pd.Series, key: tuple, cfg: dict, trials: int) -> list[dict]:
    """One row per climate present in `daily` (index: day), the interval once it has min_days days."""
    c, out = cfg["scorecard"], []
    both = pd.concat([daily.rename("ret"), trades.rename("n"), label.rename("cell")], axis=1, join="inner")
    for cell, g in both.groupby("cell"):
        x = g["ret"].to_numpy(dtype=float)
        lo, hi = metrics.block_mean_ci(x, metrics.seed_of(*key, cell), block=c["block"], level=c["level"]) \
            if len(x) >= c["min_days"] else (None, None)
        out.append({"kind": key[0], "name": key[1], "exchange": key[2], "variant": key[3], "cell": cell, "days": len(x),
                    "trades": int(g["n"].sum()), "annual": float(x.mean() * metrics.YEAR),
                    "lo": None if lo is None else lo * metrics.YEAR, "hi": None if hi is None else hi * metrics.YEAR,
                    "trials": trials})
    return out


def build(reg: pd.DataFrame, spans: dict, daily: dict, cfg: dict, trials: int,
          mixtures: dict | None = None) -> list[dict]:
    """spans: (strategy, exchange) -> (first day, last day exclusive); daily: (strategy, exchange, variant) -> frame
    of ret and trades by day; mixtures: (name, variant) -> daily return Series over the days it was scored."""
    label = labels(reg)
    out = []
    ret = reg["btc_close"].pct_change().dropna()
    out += cells(ret, pd.Series(0, index=ret.index), label, ("benchmark", "hold BTC", "", ""), cfg, trials)
    for (s, e, v), frame in sorted(daily.items()):
        if (s, e) not in spans:
            continue
        start, end = spans[(s, e)]
        days = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) - pd.Timedelta(days=1), freq="D")
        f = frame.reindex(days).fillna(0.0)
        out += cells(f["ret"], f["trades"], label, ("strategy", s, e, v), cfg, trials)
    for (name, v), ret in sorted((mixtures or {}).items()):
        out += cells(ret, pd.Series(0, index=ret.index), label, ("mixture", name, "", v), cfg, trials)
    return out


def regimes(conn) -> pd.DataFrame:
    """The `regimes` table as a frame indexed by day: btc_close, trend, vol_bucket."""
    rows = conn.execute("SELECT day, btc_close, trend, vol_bucket FROM regimes ORDER BY day").fetchall()
    return pd.DataFrame([r[1:] for r in rows], index=pd.to_datetime([r[0] for r in rows]),
                        columns=["btc_close", "trend", "vol_bucket"]).astype({"btc_close": float})


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    reg = regimes(conn)
    box = (cfg["lockbox"]["start"],)
    spans = {(s, e): (a, b) for s, e, a, b in conn.execute(SPANS, box)}
    daily: dict = {}
    for s, e, v, day, ret, n in conn.execute(DAILY, box):
        daily.setdefault((s, e, v), []).append((pd.Timestamp(day), ret, n))
    frames = {k: pd.DataFrame(v, columns=["day", "ret", "trades"]).set_index("day") for k, v in daily.items()}
    mixed: dict = {}
    for name, v, day, ret in conn.execute("SELECT m.name, m.variant, d.day, d.ret FROM mixtures m JOIN mixture_daily d "
                                          "USING (variant) ORDER BY 2, 3"):
        mixed.setdefault((name, v), {})[pd.Timestamp(day)] = ret
    trials = confidence.trial_count(conn)
    out = build(reg, spans, frames, cfg, trials, {k: pd.Series(v, dtype=float) for k, v in mixed.items()})
    with conn.transaction():
        conn.execute("DELETE FROM climate_scorecard")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO climate_scorecard ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
    return {"scorecard": len(out)}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"scorecard: {run(conn)}", flush=True)
    sys.exit(0)
