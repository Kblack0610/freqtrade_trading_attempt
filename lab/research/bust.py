#!/usr/bin/env python3
"""By hand: test BustShort on every BTC fall in the okx swap history, and print a markdown report.

python lab/research/bust.py <userdir>. Backtests only. Funding history on okx reaches about three
months, so older funding is the constant of each variant. See docs/short-bust.md.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BASKETS = {"majors": ["BTC", "ETH", "SOL"], "ai": ["NEAR", "GRT", "RENDER", "TAO", "WLD"]}
# Per 8 hours, shorts paying: about 33% a year, as in a crowded short. 0 is neutral.
FUNDING = {"neutral": 0.0, "shorts pay": 0.0003}
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


def backtest(user: Path, conf: Path) -> pd.DataFrame:
    """Every trade over the whole history, one row each. Falls are cut from this, so each has its 200 days of warm-up."""
    from freqtrade.data.btanalysis import load_backtest_stats

    with tempfile.TemporaryDirectory() as d:
        freqtrade("backtesting", "--userdir", str(user), "--config", str(conf), "--strategy", "BustShort",
                  "--strategy-path", str(HERE), "--timerange", f"{START}-", "--fee", str(FEE), "--export", "trades",
                  "--backtest-directory", d, "--cache", "none")
        trades = pd.DataFrame(load_backtest_stats(Path(d))["strategy"]["BustShort"]["trades"])
    for col in ("open_date", "close_date"):
        trades[col] = pd.to_datetime(trades[col], utc=True).dt.tz_localize(None)
    return trades


def result(trades: pd.DataFrame, rate: float) -> tuple[float, int, float]:
    """Return on the wallet with funding of `rate` per 8 hours paid on each stake, trades, and share won after funding."""
    if trades.empty:
        return 0.0, 0, 0.0
    days = (trades["close_date"] - trades["open_date"]).dt.total_seconds() / 86400
    net = trades["profit_abs"] - trades["stake_amount"] * rate * 3 * days
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

    user = Path(sys.argv[1]).resolve()
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
    print("| Basket | Window | BTC | Short, no funding | Short, shorts pay | Trades | Won |\n|---|---|---|---|---|---|---|")
    for basket, coins in BASKETS.items():
        config(conf, coins)
        trades = backtest(user, conf)
        print(f"{basket}: first trade {trades['open_date'].min()}", file=sys.stderr)
        for name, (btc, t) in windows(trades, spans).items():
            flat, n, _ = result(t, FUNDING["neutral"])
            paid, _, won = result(t, FUNDING["shorts pay"])
            print(f"| {basket} | {name} | {'' if btc is None else f'{btc:+.0%}'} | {flat:+.1%} | {paid:+.1%} | {n} | {won:.0%} |", flush=True)


if __name__ == "__main__":
    main()
