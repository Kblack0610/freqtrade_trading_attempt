#!/usr/bin/env python3
"""Pick a research venue's whitelist: its most traded spot pairs that have a long enough history.

`python lab/pairs.py <exchange> [--quote USDT] [--count 40] [--listed-by 2022-01-01] [--min-volume 2e6]`
ranks the exchange's active spot pairs in the quote currency by mean daily quote volume over the last
30 days, keeps those that trade at least --min-volume a day (a thinner book would not fill a backtest's
orders at its candle prices) and whose first daily candle is on or before --listed-by (so every pair
covers the 2022 bear market), and writes the top --count into
user_data/exchanges/<exchange>.json as pair_whitelist. Only that key changes; the file must exist.

Run it by hand and review the diff: the list is today's liquid survivors, so a refresh changes what
every past year is backtested on, and the analysis cache reruns those chunks.
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("REPO", Path(__file__).resolve().parents[1]))
# Pegged or wrapped coins track something else, and leveraged tokens decay by design.
NOT_TRADED = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "USDD", "BUSD", "USDP", "EUR", "EURT",
              "GUSD", "UST", "USTC", "USD1", "RLUSD", "WBTC", "STETH", "WSTETH", "PAXG", "XAUT"}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
VOLUME_DAYS = 30
CANDIDATES = 120  # by 24h volume, before the slower 30-day and listing checks


def tradable(market: dict, quote: str) -> bool:
    base = market["base"]
    return (market.get("spot") and market.get("active") is not False and market["quote"] == quote
            and base not in NOT_TRADED and not any(base.endswith(s) and base != s for s in LEVERAGED))


def pick(volumes: dict[str, float], firsts: dict[str, dt.date], count: int, listed_by: dt.date,
         min_volume: float) -> list[str]:
    """The `count` pairs with the most volume among those trading min_volume a day, listed by `listed_by`."""
    ok = [p for p, v in volumes.items() if v >= min_volume and firsts.get(p) and firsts[p] <= listed_by]
    return sorted(ok, key=lambda p: -volumes[p])[:count]


def survey(ex, quote: str, now: dt.datetime, listed_by: dt.date) -> tuple[dict[str, float], dict[str, dt.date]]:
    """30-day volume per candidate, and its first daily candle from two weeks before listed_by on. OKX and
    KuCoin return nothing for a start far before the first candle, so the oldest one cannot be asked for."""
    markets = [m for m in ex.load_markets().values() if tradable(m, quote)]
    tickers = ex.fetch_tickers([m["symbol"] for m in markets]) if ex.has.get("fetchTickers") else {}
    by_24h = sorted(markets, key=lambda m: -((tickers.get(m["symbol"]) or {}).get("quoteVolume") or 0))[:CANDIDATES]
    since = int((now - dt.timedelta(days=VOLUME_DAYS + 1)).timestamp() * 1000)
    probe = int(dt.datetime.combine(listed_by - dt.timedelta(days=14), dt.time(), dt.timezone.utc).timestamp() * 1000)
    volumes, firsts = {}, {}
    for m in by_24h:
        p = m["symbol"]
        days = ex.fetch_ohlcv(p, "1d", since=since, limit=VOLUME_DAYS + 1)[:-1]  # the last day is still open
        volumes[p] = sum(c[4] * c[5] for c in days) / max(len(days), 1)
        first = ex.fetch_ohlcv(p, "1d", since=probe, limit=1)
        if first:
            firsts[p] = dt.datetime.fromtimestamp(first[0][0] / 1000, dt.timezone.utc).date()
    return volumes, firsts


def write(exchange: str, pairs: list[str]) -> Path:
    path = REPO / "user_data" / "exchanges" / f"{exchange}.json"
    cfg = json.loads(path.read_text())
    cfg["exchange"]["pair_whitelist"] = pairs
    path.write_text(json.dumps(cfg, indent=4) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    import ccxt

    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("exchange")
    a.add_argument("--quote", default="USDT")
    a.add_argument("--count", type=int, default=40)
    a.add_argument("--listed-by", type=dt.date.fromisoformat, default=dt.date(2022, 1, 1))
    a.add_argument("--min-volume", type=float, default=2e6)
    args = a.parse_args(argv)
    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True})
    volumes, firsts = survey(ex, args.quote, dt.datetime.now(dt.timezone.utc), args.listed_by)
    pairs = pick(volumes, firsts, args.count, args.listed_by, args.min_volume)
    for p in pairs:
        print(f"{p:<14} {volumes[p] / 1e6:10.1f}M/day")
    young = [p for p in sorted(volumes, key=lambda p: -volumes[p]) if volumes[p] >= args.min_volume and p not in pairs]
    if young:
        print(f"listed after {args.listed_by}, left out: {' '.join(young)}")
    print(f"wrote {len(pairs)} pairs to {write(args.exchange, pairs)}")
    return 0 if pairs else 1


if __name__ == "__main__":
    sys.exit(main())
