#!/usr/bin/env python3
"""The strategy book: what each scored strategy might return, beside holding BTC, and what a stage costs.

Advice for a human, like lab/confidence.py: nothing here touches a bot, and lab/league.py refuses real money.
A strategy gets a range only with a confidence score and at least [confidence] min_folds walk-forward months.
Its basis is those months' tuned wallet returns, the months hyperopt never saw, resampled in runs of
[book] block_months. Hold is BTC over the same months. A range assumes the tested months repeat: it says
nothing about a market they did not contain, and 12 months cannot carry a 5-year range with any firmness.

Run alone with `python lab/book.py`; lab/analyze.py and lab/walkforward.py run it after the score.
"""

import sys

import pandas as pd

import metrics
import suggest
from db import connect

FOLDS = """
SELECT w.strategy, w.exchange, w.fold_start, w.test_end, coalesce(w.tuned_profit, 0)
FROM walkforward w JOIN confidence c USING (strategy, exchange)
WHERE c.score IS NOT NULL AND w.ok AND w.params IS NOT NULL AND w.test_end <= %s ORDER BY 1, 2, 3
"""
COLS = ["strategy", "exchange", "basis", "horizon_months", "p5", "p50", "p95", "prob_loss", "exp_max_drawdown", "paths",
        "basis_months"]


def hold_months(folds: list[tuple], close: pd.Series) -> list[float]:
    """BTC's return over each fold's month, or nothing when a month has no close to start from."""
    day = pd.Timedelta(days=1)
    start = [close.asof(pd.Timestamp(f[0]) - day) for f in folds]
    end = [close.asof(pd.Timestamp(f[1]) - day) for f in folds]
    if pd.isna(start).any() or pd.isna(end).any():
        return []
    return [b / a - 1.0 for a, b in zip(start, end)]


def rows(folds: dict, close: pd.Series, cfg: dict) -> list[dict]:
    """folds: (strategy, exchange) -> [(fold_start, test_end, monthly return)] for scored pairs, in month order."""
    c = cfg["book"]
    out = []
    for (s, e), f in folds.items():
        if len(f) < cfg["confidence"]["min_folds"]:
            continue
        for basis, monthly in (("strategy", [x[2] for x in f]), ("hold", hold_months(f, close))):
            for h in c["horizon_months"] if monthly else ():
                out.append({"strategy": s, "exchange": e, "basis": basis, "horizon_months": h, "basis_months": len(monthly),
                            **metrics.projection(monthly, h, metrics.seed_of(s, e, basis, h), block=c["block_months"])})
    return out


def tax_rate(days: int, cfg: dict) -> float:
    return cfg["book"]["tax_long"] if days > 365 else cfg["book"]["tax_short"]


def after_tax(growth: float, days: int, cfg: dict) -> float:
    """What one lot's growth multiple leaves after tax on selling it."""
    return growth - max(growth - 1.0, 0.0) * tax_rate(days, cfg)


def lot(close: pd.Series, start, cfg: dict) -> tuple[float, float]:
    """One buy at `start` held to the last close, net of a fee each way: its growth, and that after tax."""
    growth = close.iloc[-1] / close[start] * (1.0 - cfg["book"]["benchmark_fee"]) ** 2
    return growth, after_tax(growth, (close.index[-1] - start).days, cfg)


def trend_filter(close: pd.Series, long_mean: pd.Series, cfg: dict) -> tuple[pd.Series, float, int]:
    """BTC while yesterday's close is above its long mean, else cash, selling on the last day: the
    equity curve, the wallet after tax with losses carried to later gains, and how many trades it took."""
    fee = cfg["book"]["benchmark_fee"]
    held = (close > long_mean).shift(1, fill_value=False)
    flips = held.astype(int).diff().abs().fillna(0.0).astype(bool)
    flips.iloc[-1] = flips.iloc[-1] or held.iloc[-1]
    equity = ((close.pct_change().fillna(0.0) * held + 1.0) * (1.0 - fee * flips)).cumprod()
    wallet, losses, before, base, start = 1.0, 0.0, 1.0, None, None
    for day, flip, value in zip(close.index, flips, equity):
        if flip and base is None:
            base, start = before, day
        elif flip:
            gain = wallet * (value / base - 1.0)
            used = min(losses, max(gain, 0.0))
            wallet += gain - max(gain - used, 0.0) * tax_rate((day - start).days, cfg)
            losses += max(-gain, 0.0) - used
            base = None
        before = value
    return equity, wallet, int(flips.sum())


def dca(close: pd.Series, cfg: dict) -> tuple[pd.Series, float, float]:
    """The same amount into BTC on each month's first day: value per dollar paid in, and the final
    return before and after tax."""
    firsts = close.groupby([close.index.year, close.index.month]).head(1).index
    units = (1.0 / close[firsts]).reindex(close.index, fill_value=0.0).cumsum()
    paid = pd.Series(1.0, index=firsts).reindex(close.index, fill_value=0.0).cumsum()
    lots = [lot(close, d, cfg) for d in firsts]
    return units * close / paid, sum(g for g, _ in lots) / len(lots) - 1.0, sum(t for _, t in lots) / len(lots) - 1.0


def drawdown(equity: pd.Series) -> float:
    return float((1.0 - equity / equity.cummax()).max())


def benchmark_rows(btc: pd.Series, eth: pd.Series, cfg: dict) -> list[dict]:
    """The plain alternatives to an active strategy, over the last few years and the whole shared history.

    btc and eth: daily closes indexed by date. Every benchmark in a window starts on the same day,
    after the long mean has enough days. Tax rates are guesses in [book], and this is not tax advice.
    """
    c = cfg["book"]
    days = btc.index.intersection(eth.index)
    if len(days) <= c["trend_days"]:
        return []
    btc, eth = (s.loc[days].astype(float).set_axis(pd.to_datetime(days)).sort_index() for s in (btc, eth))
    long_mean = btc.rolling(c["trend_days"]).mean()
    first, end = long_mean.first_valid_index(), btc.index[-1]
    spans = {f"{y}y": end - pd.DateOffset(years=y) for y in c["benchmark_years"]} | {"all": first}
    out = []
    for win, start in spans.items():
        if start < first:
            continue
        b, e = btc[start:], eth[start:]
        years = (end - b.index[0]).days / 365.25
        t0 = b.index[0]
        hold_after = lot(b, t0, cfg)
        pair = [lot(b, t0, cfg), lot(e, t0, cfg)]
        trend, trend_after, trades = trend_filter(b, long_mean[start:], cfg)
        dca_curve, dca_total, dca_after = dca(b, cfg)
        rows = [("Hold BTC", b / b.iloc[0], hold_after[0] - 1.0, hold_after[1] - 1.0, 2),
                ("Hold BTC and ETH, half each", 0.5 * (b / b.iloc[0] + e / e.iloc[0]), sum(g for g, _ in pair) / 2 - 1.0,
                 sum(t for _, t in pair) / 2 - 1.0, 4),
                ("Monthly DCA into BTC", dca_curve, dca_total, dca_after, None),
                (f"BTC above its {c['trend_days']}-day mean", trend, float(trend.iloc[-1]) - 1.0, trend_after - 1.0, trades)]
        for name, curve, total, taxed, n in rows:
            out.append({"name": name, "win": win, "start_day": b.index[0].date(), "end_day": end.date(), "total_return": total,
                        "cagr": None if name.startswith("Monthly") else (1.0 + total) ** (1.0 / years) - 1.0,
                        "max_drawdown": drawdown(curve), "after_tax_return": taxed, "trades": n})
    return out


def budget(cfg: dict) -> dict:
    """Each stage's capital and what it can lose at each limit, and the yearly cost of running at all."""
    c = cfg["book"]
    vps = 12 * c["vps_per_month"]
    stages = [{**s, "at_risk": s["stake"] * s["max_open"], "day_loss": s["capital"] * cfg["watcher"]["day_loss"],
               "hard_stop": s["capital"] * c["hard_stop"], "vps_share": vps / s["capital"]} for s in c["stages"]]
    return {"stages": stages, "vps_year": vps, "cpa_once": c["cpa_once"]}


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    folds: dict = {}
    for s, e, start, end, profit in conn.execute(FOLDS, (cfg["lockbox"]["start"],)):
        folds.setdefault((s, e), []).append((start, end, profit))
    days = conn.execute("SELECT day, btc_close FROM regimes ORDER BY day").fetchall()
    close = pd.Series([c for _, c in days], index=pd.to_datetime([d for d, _ in days]), dtype=float)
    out = rows(folds, close, cfg)
    with conn.transaction():
        conn.execute("DELETE FROM book")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO book ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
    return {"book": len({(r["strategy"], r["exchange"]) for r in out})}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"book: {run(conn)}", flush=True)
    sys.exit(0)
