#!/usr/bin/env python3
"""By hand: test a research strategy on every BTC fall in the okx swap history, and print a markdown report.

python lab/research/bust.py <userdir> [strategy]. Backtests only; the strategy is BustShort unless named.
Funding is measured, from funding_rates (lab/futures.py, DATABASE_URL): okx's own series where it covers
a trade's whole hold, else kucoin's for the same swap. A trade neither covers is counted apart, with no
funding guessed for it. See docs/short-bust.md.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BASKETS = {"majors": ["BTC", "ETH", "SOL"], "ai": ["NEAR", "GRT", "RENDER", "TAO", "WLD"]}
# Funding sources in order of preference: the venue the candles come from, then a proxy for the same swap.
FUNDING_VENUES = ("okx", "kucoin")
FEE = 0.001  # Per side: okx's 0.05% taker from ccxt plus a 0.05% slippage guess.
START, DROP, WALLET = "20200101", 0.25, 1000


def falls(close: pd.Series, drop: float = DROP) -> list[tuple]:
    """Each peak-to-trough fall of at least `drop`, ended by a rise of `drop` from its trough."""
    out, peak, trough, falling = [], close.index[0], close.index[0], False
    for day, price in close.items():
        if not falling:
            if price >= close[peak]:
                peak = day
            elif price <= close[peak] * (1 - drop):
                falling, trough = True, day
        elif price < close[trough]:
            trough = day
        elif price >= close[trough] * (1 + drop):
            out.append((peak, trough))
            falling, peak = False, day
    if falling:
        out.append((peak, trough))
    return out


def pair(coin: str) -> str:
    return f"{coin}/USDT:USDT"


def config(path: Path, coins: list[str]) -> None:
    """A fixed stake per coin, so a fall's result does not depend on what came before it."""
    path.write_text(json.dumps({
        "trading_mode": "futures", "margin_mode": "isolated", "dry_run": True, "dry_run_wallet": WALLET,
        "stake_currency": "USDT", "stake_amount": WALLET / len(coins) * 0.99, "max_open_trades": len(coins),
        "futures_funding_rate": 0, "entry_pricing": {"price_side": "other"}, "exit_pricing": {"price_side": "other"},
        "pairlists": [{"method": "StaticPairList"}],
        "exchange": {"name": "okx", "key": "", "secret": "", "pair_whitelist": [pair(c) for c in coins]},
    }))


def freqtrade(*args: str) -> None:
    done = subprocess.run(["freqtrade", *args], capture_output=True, text=True)
    if done.returncode:
        sys.exit(done.stdout[-1500:] + done.stderr[-1500:])


def backtest(user: Path, conf: Path, strategy: str) -> pd.DataFrame:
    """Every trade over the whole history, one row each. Falls are cut from this, so each has its 200 days of warm-up."""
    from freqtrade.data.btanalysis import load_backtest_stats

    with tempfile.TemporaryDirectory() as d:
        freqtrade("backtesting", "--userdir", str(user), "--config", str(conf), "--strategy", strategy,
                  "--strategy-path", str(HERE), "--timerange", f"{START}-", "--fee", str(FEE), "--export", "trades",
                  "--backtest-directory", d, "--cache", "none")
        trades = pd.DataFrame(load_backtest_stats(Path(d))["strategy"][strategy]["trades"])
    for col in ("open_date", "close_date"):
        trades[col] = pd.to_datetime(trades[col], utc=True).dt.tz_localize(None)
    return trades


def load_funding(conn, pairs: list[str]) -> pd.DataFrame:
    """Every stored settlement for these swaps: exchange, pair, ts (naive UTC), rate."""
    rows = conn.execute("SELECT exchange, pair, ts, rate FROM funding_rates WHERE pair = ANY(%s) ORDER BY ts",
                        (pairs,)).fetchall()
    out = pd.DataFrame(rows, columns=["exchange", "pair", "ts", "rate"])
    out["ts"] = pd.to_datetime(out["ts"], utc=True).dt.tz_localize(None)
    return out


def measured(trades: pd.DataFrame, rates: pd.DataFrame) -> pd.DataFrame:
    """Each trade with `funding`, what it received (positive) or paid on its stake, from the first venue in
    FUNDING_VENUES whose series starts by the trade's open, and `funding_from`, that venue. A positive rate is
    paid by longs to shorts. Both are NaN for a trade no venue covers. Freqtrade's own funding_fees, from
    whatever funding candles it downloaded, are taken out of profit_abs into `gross`, so nothing is counted twice."""
    out = trades.copy()
    out["gross"] = out["profit_abs"] - out.get("funding_fees", 0.0)
    out["funding"], out["funding_from"] = float("nan"), None
    starts = rates.groupby(["exchange", "pair"])["ts"].min()
    for i, t in out.iterrows():
        for venue in FUNDING_VENUES:
            first = starts.get((venue, t["pair"]))
            if first is None or first > t["open_date"]:
                continue
            r = rates[(rates["exchange"] == venue) & (rates["pair"] == t["pair"])
                      & (rates["ts"] > t["open_date"]) & (rates["ts"] <= t["close_date"])]["rate"].sum()
            side = 1.0 if t.get("is_short", True) else -1.0
            out.loc[i, ["funding", "funding_from"]] = side * t["stake_amount"] * r, venue
            break
    return out


def result(trades: pd.DataFrame, funded: bool) -> tuple[float, int, float]:
    """Return on the wallet, trades, and share won. Funded: only trades with measured funding, net of it."""
    t = trades[trades["funding"].notna()] if funded else trades
    if t.empty:
        return 0.0, 0, 0.0
    net = t["gross"] + (t["funding"] if funded else 0.0)
    return float(net.sum() / WALLET), len(net), float((net > 0).mean())


def windows(trades: pd.DataFrame, spans: list[tuple]) -> dict:
    """Trades opened in each fall, those opened outside every fall, and all of them."""
    opened = trades["open_date"].dt.date
    inside = [(opened >= a) & (opened <= b) for a, b, _ in spans]
    out = {f"{a} to {b}": (btc, trades[m]) for (a, b, btc), m in zip(spans, inside)}
    out["outside every fall"] = (None, trades[~pd.concat(inside, axis=1).any(axis=1)])
    return out | {"whole history": (None, trades)}


def main() -> None:
    from freqtrade.data.history import load_pair_history
    from freqtrade.enums import CandleType

    from lab.db import connect

    user, strategy = Path(sys.argv[1]).resolve(), (sys.argv[2:] or ["BustShort"])[0]
    (user / "data").mkdir(parents=True, exist_ok=True)
    conf = user / "bust.json"
    if not (user / "data" / "okx" / "futures").exists():
        config(conf, BASKETS["majors"] + BASKETS["ai"])
        freqtrade("download-data", "--userdir", str(user), "--config", str(conf), "--timeframes", "1d",
                  "--timerange", f"{START}-", "--trading-mode", "futures")
    candles = load_pair_history(pair("BTC"), "1d", user / "data" / "okx", candle_type=CandleType.FUTURES)
    close = candles.set_index(candles["date"].dt.date)["close"]
    print(f"BTC swap candles {close.index[0]} to {close.index[-1]}\n")
    spans = [(a, b, close[b] / close[a] - 1) for a, b in falls(close)]
    with connect() as conn:
        rates = load_funding(conn, [pair(c) for coins in BASKETS.values() for c in coins])
    print(f"{strategy}. Measured funding: " + "; ".join(
        f"{v} {p} from {g.min():%Y-%m-%d}" for (v, p), g in rates.groupby(["exchange", "pair"])["ts"]) + "\n")
    print("| Basket | Window | BTC | No funding | Trades | Won | Measured trades, no funding | Same, measured funding | Measured trades | Won |\n"
          "|---|---|---|---|---|---|---|---|---|---|")
    for basket, coins in BASKETS.items():
        config(conf, coins)
        trades = measured(backtest(user, conf, strategy), rates)
        print(f"{basket}: first trade {trades['open_date'].min()}; funding from "
              f"{trades['funding_from'].value_counts(dropna=False).to_dict()}", file=sys.stderr)
        for name, (btc, t) in windows(trades, spans).items():
            flat, n, won = result(t, False)
            cover = t[t["funding"].notna()]
            bare, _, _ = result(cover.assign(funding=0.0), True)
            paid, m, mwon = result(t, True)
            print(f"| {basket} | {name} | {'' if btc is None else f'{btc:+.0%}'} | {flat:+.1%} | {n} | {won:.0%} "
                  f"| {bare:+.1%} | {paid:+.1%} | {m} | {mwon:.0%} |", flush=True)


if __name__ == "__main__":
    main()
