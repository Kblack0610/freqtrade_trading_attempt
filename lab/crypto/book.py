#!/usr/bin/env python3
"""The strategy book: what each scored strategy might return, beside holding BTC, and what a stage costs.

Advice for a human, like lab/crypto/confidence.py: nothing here touches a bot, and lab/crypto/league.py refuses real money.
A strategy gets a range only with a confidence score and at least [confidence] min_folds walk-forward months.
Its basis is those months' tuned wallet returns, the months hyperopt never saw, resampled in runs of
[book] block_months. Hold is BTC over the same months. A range assumes the tested months repeat: it says
nothing about a market they did not contain, and 12 months cannot carry a 5-year range with any firmness.

Run alone with `python lab/crypto/book.py`; lab/crypto/analyze.py and lab/crypto/walkforward.py run it after the score.
"""

import math
import sys

import pandas as pd

from lab import metrics
from lab.core import readiness
from lab.crypto import suggest
from lab.core.db import connect

FOLDS = f"""
SELECT w.strategy, w.exchange, w.fold_start, w.test_end, coalesce(w.tuned_profit, 0)
FROM walkforward w JOIN ({readiness.CRYPTO_ROWS}) c USING (strategy, exchange)
WHERE c.score IS NOT NULL AND w.ok AND w.params IS NOT NULL AND w.test_end <= %s ORDER BY 1, 2, 3
"""
COLS = ["strategy", "exchange", "basis", "horizon_months", "p5", "p50", "p95", "prob_loss", "exp_max_drawdown", "paths",
        "basis_months"]
DAYS_PER_YEAR = 365  # crypto trades every day: annualizes daily volatility

STOP_COLS = ["strategy", "exchange", "stage", "horizon_months", "hard_stop", "p_hard_stop", "basis_months"]
STOP_HORIZON = 12


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


def stop_rows(folds: dict, cfg: dict) -> list[dict]:
    """Per scored strategy and stage: the share of its 12-month out-of-sample paths (those behind exp_max_drawdown)
    whose fall from peak reaches [book] hard_stop, a share of capital. Every stage shares that share, so it is bootstrapped once per pair
    and written under each stage's name."""
    c = cfg["book"]
    out = []
    for (s, e), f in folds.items():
        if len(f) < cfg["confidence"]["min_folds"]:
            continue
        monthly = [x[2] for x in f]
        share = metrics.hard_stop_share(monthly, STOP_HORIZON, metrics.seed_of(s, e, "strategy", STOP_HORIZON),
                                        c["hard_stop"], block=c["block_months"])
        out += [{"strategy": s, "exchange": e, "stage": st["name"], "horizon_months": STOP_HORIZON, "hard_stop": c["hard_stop"],
                 "basis_months": len(monthly), "p_hard_stop": share} for st in c["stages"]]
    return out


def tax_rate(days: int, cfg: dict) -> float:
    return cfg["book"]["tax_long"] if days > 365 else cfg["book"]["tax_short"]


def after_tax(growth: float, days: int, cfg: dict) -> float:
    """What one lot's growth multiple leaves after tax on selling it."""
    return growth - max(growth - 1.0, 0.0) * tax_rate(days, cfg)


def median_after_tax(band: dict, cfg: dict) -> float:
    """A book row's median after tax. A strategy's gains are short term and taxed every year; hold pays once, on selling."""
    months, growth = band["horizon_months"], 1.0 + band["p50"]
    if band["basis"] == "hold":
        return after_tax(growth, round(months * 365 / 12), cfg) - 1.0
    years = months / 12
    return after_tax(growth ** (1.0 / years), 0, cfg) ** years - 1.0


def hurdle(cfg: dict) -> list[dict]:
    """What T-bills return over each horizon at today's yield, before and after tax on the interest."""
    c = cfg["book"]
    return [{"months": h, "total": (1.0 + c["tbill_yield"]) ** (h / 12) - 1.0,
             "after_tax": (1.0 + c["tbill_yield"] * (1.0 - c["tax_short"])) ** (h / 12) - 1.0} for h in c["horizon_months"]]


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


def basket_sigma(closes: pd.DataFrame) -> float:
    """Annualized volatility of an equal-weight basket's daily returns: one column of daily closes per coin.
    The bots are one bet (lab docs/risk.md section 2), so their sizing takes the basket's sigma, not one pair's."""
    daily = closes.astype(float).sort_index().pct_change(fill_method=None).mean(axis=1).dropna()
    return float(daily.std() * math.sqrt(DAYS_PER_YEAR))


def kelly_ceiling(capital: float, mu: float, sigma: float, years: float, cfg: dict) -> float:
    """The most a stage may hold in open trades: [book] kelly_fraction of continuous Kelly, mu / sigma^2, on the
    lower bound of the edge, mu - z sigma / sqrt(T). Zero until T > (z / Sharpe)^2; never above capital."""
    c = cfg["book"]
    lower = mu - c["kelly_z"] * sigma / math.sqrt(years) if years > 0 else 0.0
    return capital * min(c["kelly_fraction"] * max(lower, 0.0) / sigma ** 2, 1.0)


def budget(cfg: dict, mu: float | None = None, sigma: float | None = None, years: float | None = None) -> dict:
    """Each stage's capital and what it can lose at each limit, and the yearly cost of running at all.

    mu: the annual edge the step rests on; sigma: basket_sigma; years: the length of the record behind mu. With all
    three finite and sigma above zero, each stage gets a kelly_ceiling and over_kelly marks one whose open trades
    would exceed it; else both are None, since an unknown ceiling must not read as within it.
    """
    c = cfg["book"]
    vps = 12 * c["vps_per_month"]
    known = all(x is not None and math.isfinite(x) for x in (mu, sigma, years)) and sigma > 0
    stages = []
    for s in c["stages"]:
        at_risk = s["stake"] * s["max_open"]
        ceiling = kelly_ceiling(s["capital"], mu, sigma, years, cfg) if known else None
        stages.append({**s, "at_risk": at_risk, "day_loss": s["capital"] * cfg["watcher"]["day_loss"],
                       "hard_stop": s["capital"] * c["hard_stop"], "vps_share": vps / s["capital"],
                       "kelly_ceiling": ceiling, "over_kelly": None if ceiling is None else at_risk > ceiling})
    return {"stages": stages, "vps_year": vps, "cpa_once": c["cpa_once"]}


def run(conn, cfg: dict | None = None) -> dict:
    cfg = cfg or suggest.load_config()
    folds: dict = {}
    for s, e, start, end, profit in conn.execute(FOLDS, (cfg["lockbox"]["start"],)):
        folds.setdefault((s, e), []).append((start, end, profit))
    days = conn.execute("SELECT day, btc_close FROM regimes ORDER BY day").fetchall()
    close = pd.Series([c for _, c in days], index=pd.to_datetime([d for d, _ in days]), dtype=float)
    out, stops = rows(folds, close, cfg), stop_rows(folds, cfg)
    with conn.transaction():
        conn.execute("DELETE FROM book")
        conn.execute("DELETE FROM book_stops")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO book ({', '.join(COLS)}) VALUES ({', '.join(f'%({k})s' for k in COLS)})", out)
            cur.executemany(f"INSERT INTO book_stops ({', '.join(STOP_COLS)}) VALUES ({', '.join(f'%({k})s' for k in STOP_COLS)})",
                            stops)
    return {"book": len({(r["strategy"], r["exchange"]) for r in out})}


if __name__ == "__main__":
    with connect(autocommit=True) as conn:
        print(f"book: {run(conn)}", flush=True)
    sys.exit(0)
