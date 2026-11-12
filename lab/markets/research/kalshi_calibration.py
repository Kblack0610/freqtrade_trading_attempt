"""By hand: is any Kalshi price bucket mispriced enough to beat the taker fee? Dry run, no keys, no orders.

  python -m lab.markets.research.kalshi_calibration fetch [--days 60]   # settled markets + hourly candles
  python -m lab.markets.research.kalshi_calibration report              # calibration tables, markdown
  python -m lab.markets.research.kalshi_calibration open                # open markets that fit a surviving cell

Reads only Kalshi's public market-data API. Data caches under ~/.cache/trader-markets/kalshi/.
Entry is the taker price at a fixed horizon before the scheduled expiration: the yes ask to buy YES, one
minus the yes bid to buy NO. Pnl per contract is outcome - entry - fee. The bootstrap resamples
whole events, since the markets of one event settle together.
"""

import argparse
import hashlib
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import requests

from lab.markets.api import Api
from lab.markets.recorder import load_config

CACHE = Path.home() / ".cache" / "trader-markets" / "kalshi"
HORIZONS_H = (24, 6, 1)
BUCKETS = [(lo / 100, (lo + 10) / 100) for lo in range(0, 100, 10)]
# Kalshi's general taker fee: ceil(0.07 * C * P * (1 - P)) dollars per order, rounded up to a cent.
FEE_RATE = 0.07
STAKE = 10.0  # Dollars per trade for the fee's cent rounding; the brief's bet size.
CATEGORIES = {"sports": {"Sports"}, "econ": {"Economics"}, "politics": {"Politics", "Elections"},
              "weather": {"Climate and Weather"}}
MIN_VOLUME = 100  # Contracts traded over the market's life; thinner markets have no real quote.
BATCH = 100  # Tickers per batch candlestick call.
PER_SERIES = 400  # Most markets fetched per series, so a few busy series do not make up the sample.
MIN_N = 30  # Markets in a cell before it is scored at all.
BOOT = 2000
# Paced by lab/markets/api.py Api with [research.kalshi_calibration] in lab/markets.toml; the four candle threads
# share the one client, so they share its pace.
_cfg = load_config()
API = Api("kalshi", {**{k: v for k, v in _cfg["venues"]["kalshi"].items() if k != "budget_rps"},
                     **_cfg["research"]["kalshi_calibration"]})


def get(path: str, **params) -> dict:
    return API.get(path, params)


def ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def fee(price: float, stake: float = STAKE, mult: float = 1.0) -> float:
    """Taker fee per contract for a `stake`-dollar order at `price`, with Kalshi's cent rounding.

    `mult` is the series' fee_multiplier (1 for nearly all series, 0.5 for a few index markets).
    """
    if not 0 < price < 1:
        return 0.0
    contracts = max(1, math.floor(stake / price))
    return math.ceil(mult * FEE_RATE * contracts * price * (1 - price) * 100 - 1e-9) / 100 / contracts


def category(name: str) -> str:
    return next((k for k, v in CATEGORIES.items() if name in v), "other")


def entry_ts(m: dict, h: int) -> int | None:
    """When a trade `h` hours before the scheduled expiration is placed, or None if the market had closed.

    The anchor is expected_expiration_time, set when the market is listed. Counting back from
    close_time would be lookahead: a market that closes early closes because the news is out.
    Keeping only markets still open at entry conditions on what was known at entry, nothing later.
    """
    at = ts(m["expected_expiration_time"]) - h * 3600
    return at if ts(m.get("open_time") or m["close_time"]) <= at < ts(m["close_time"]) else None


def usable(m: dict) -> bool:
    return entry_ts(m, max(HORIZONS_H)) is not None or entry_ts(m, min(HORIZONS_H)) is not None


def quote_at(candles: list[dict], at: int) -> tuple[float, float] | None:
    """(yes bid, yes ask) of the last hourly candle ending at or before `at`, both sides quoted."""
    best = None
    for c in candles:
        if c["end_period_ts"] <= at:
            best = c
    if best is None or best["end_period_ts"] < at - 3600:
        return None
    bid, ask = float(best["yes_bid"]["close_dollars"]), float(best["yes_ask"]["close_dollars"])
    if bid <= 0 or ask >= 1 or ask <= bid:
        return None
    return bid, ask


def rows(markets: list[dict], candles: dict[str, list]) -> list[dict]:
    """One row per market, side and horizon: entry price, outcome and net pnl per contract."""
    out = []
    for m in markets:
        cs = candles.get(m["ticker"])
        if not cs:
            continue
        yes = 1.0 if m["result"] == "yes" else 0.0
        for h in HORIZONS_H:
            at = entry_ts(m, h)
            q = None if at is None else quote_at(cs, at)
            if q is None:
                continue
            bid, ask = q
            for side, entry, won in (("yes", ask, yes), ("no", 1 - bid, 1 - yes)):
                out.append({"event": m["event_ticker"], "cat": m["cat"], "h": h, "side": side,
                            "entry": entry, "won": won,
                            "pnl": won - entry - fee(entry, mult=m.get("fee_mult", 1.0))})
    return out


def boot_ci(pnl: np.ndarray, groups: np.ndarray, alpha: float, rng) -> tuple[float, float]:
    """Percentile CI of mean pnl, resampling whole events."""
    uniq, inv = np.unique(groups, return_inverse=True)
    sums, counts = np.bincount(inv, pnl), np.bincount(inv)
    k = len(uniq)
    pick = rng.integers(0, k, size=(BOOT, k))
    means = sums[pick].sum(1) / counts[pick].sum(1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def win_lower(wins: int, n: int, alpha: float) -> float:
    """One-sided Clopper-Pearson lower bound on the win rate: the p where P(X >= wins | n, p) = alpha.

    The bootstrap cannot see a loss a cell never had (74 wins in 74 resample to 74 in 74), so a
    cell must also clear break-even on this exact binomial bound.
    """
    if wins == 0:
        return 0.0
    k = np.arange(wins, n + 1)
    logc = np.array([math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) for i in k])
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        tail = np.exp(logc + k * math.log(mid) + (n - k) * math.log1p(-mid)).sum()
        lo, hi = (lo, mid) if tail > alpha else (mid, hi)
    return lo


def cells(data: list[dict]) -> list[dict]:
    """Every cut looked at: category (and all) x horizon x side x entry bucket."""
    out = []
    for cat in ["all", *CATEGORIES]:
        for h in HORIZONS_H:
            for side in ("yes", "no"):
                for lo, hi in BUCKETS:
                    sel = [r for r in data if (cat == "all" or r["cat"] == cat) and r["h"] == h
                           and r["side"] == side and lo <= r["entry"] < hi]
                    out.append({"cat": cat, "h": h, "side": side, "lo": lo, "hi": hi, "rows": sel})
    return out


def score(all_cells: list[dict], seed: int = 7) -> tuple[list[dict], int]:
    rng = np.random.default_rng(seed)
    scored = [c for c in all_cells if len(c["rows"]) >= MIN_N]
    trials = len(all_cells)
    for c in scored:
        pnl = np.array([r["pnl"] for r in c["rows"]])
        ev = np.array([r["event"] for r in c["rows"]])
        c["n"], c["events"] = len(pnl), len(set(ev))
        c["price"] = float(np.mean([r["entry"] for r in c["rows"]]))
        c["win"] = float(np.mean([r["won"] for r in c["rows"]]))
        c["mean"] = float(pnl.mean())
        c["lo95"], c["hi95"] = boot_ci(pnl, ev, 0.05, rng)
        # Bonferroni over every cell defined, scored or not: the bar a cell must clear to count.
        c["lo_adj"], _ = boot_ci(pnl, ev, 0.05 / trials, rng) if c["lo95"] > 0 else (c["lo95"], 0)
        c["breakeven"] = float(np.mean([r["won"] - r["pnl"] for r in c["rows"]]))  # entry + fee
        c["win_lo_adj"] = win_lower(int(sum(r["won"] for r in c["rows"])), c["n"], 0.05 / trials)
        c["survives"] = c["lo_adj"] > 0 and c["win_lo_adj"] > c["breakeven"]
        del c["rows"]
    return scored, trials


def sample(markets: list[dict], cap: int = PER_SERIES) -> list[dict]:
    """Up to `cap` usable markets per series, picked by a stable hash of the ticker."""
    by = {}
    for m in sorted((m for m in markets if usable(m)), key=lambda m: hashlib.sha1(m["ticker"].encode()).hexdigest()):
        by.setdefault(m["series"], []).append(m)
    return [m for ms in by.values() for m in ms[:cap]]


def fetch(days: int, reuse: bool):
    CACHE.mkdir(parents=True, exist_ok=True)
    mpath, cpath = CACHE / "markets.jsonl", CACHE / "candles.jsonl"
    since = int(time.time()) - days * 86400
    markets, cursor, pages = [], None, 0
    paging = not (reuse and mpath.exists())
    if not paging:
        markets = [json.loads(line) for line in mpath.open()]
    while paging:
        params = {"status": "settled", "limit": 200, "with_nested_markets": "true", "min_close_ts": since}
        if cursor:
            params["cursor"] = cursor
        d = get("/events", **params)
        pages += 1
        for e in d.get("events", []):
            for m in e.get("markets") or []:
                if m.get("result") in ("yes", "no") and float(m.get("volume_fp") or 0) >= MIN_VOLUME \
                        and ts(m["close_time"]) >= since:
                    m["cat"], m["category"], m["series"] = category(e["category"]), e["category"], e["series_ticker"]
                    markets.append({k: m.get(k) for k in ("ticker", "event_ticker", "series", "category", "cat", "result",
                                                      "close_time", "expected_expiration_time", "open_time",
                                                      "volume_fp", "title")})
        cursor = d.get("cursor")
        if pages % 20 == 0:
            print(f"events page {pages}, {len(markets)} markets", file=sys.stderr, flush=True)
        if not cursor or not d.get("events"):
            mpath.write_text("".join(json.dumps(m) + "\n" for m in markets))
            paging = False
    print(file=sys.stderr)
    have = set()
    if cpath.exists():
        have = {json.loads(line)["ticker"] for line in cpath.open()}
    todo = [m for m in sample(markets) if m["ticker"] not in have]
    # One batch call per expiration time: markets that expire together share a candle window.
    groups = {}
    for m in todo:
        groups.setdefault(m["expected_expiration_time"], []).append(m["ticker"])
    batches = [(exp, names[i:i + BATCH]) for exp, names in groups.items() for i in range(0, len(names), BATCH)]
    lock, done, failed = threading.Lock(), [0], []

    def one(batch):
        exp, tickers = ts(batch[0]), batch[1]
        try:
            d = get("/markets/candlesticks", market_tickers=",".join(tickers),
                    start_ts=exp - (max(HORIZONS_H) + 1) * 3600, end_ts=exp, period_interval=60)
        except requests.HTTPError as err:
            with lock:
                failed.append(len(tickers))
            print(f"batch failed, {len(tickers)} markets left for a rerun: {err.response.status_code}",
                  file=sys.stderr, flush=True)
            return
        lines = "".join(json.dumps({"ticker": x["market_ticker"], "candles": [
            {"end_period_ts": c["end_period_ts"], "yes_bid": c["yes_bid"], "yes_ask": c["yes_ask"]}
            for c in x.get("candlesticks", [])]}) + "\n" for x in d.get("markets", []))
        with lock:
            with cpath.open("a") as f:
                f.write(lines)
            done[0] += 1
            if done[0] % 500 == 0:
                print(f"candles {done[0]}/{len(batches)} batches", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, batches))
    print(f"candles done, {len(todo) - sum(failed)} fetched, {len(failed)} batches failed "
          f"({sum(failed)} markets; rerun with --reuse to retry them)", file=sys.stderr)


def fee_multipliers() -> dict[str, float]:
    path = CACHE / "series.json"
    if not path.exists():
        path.write_text(json.dumps(get("/series", limit=5000)))
    return {x["ticker"]: float(x.get("fee_multiplier") or 1) for x in json.loads(path.read_text())["series"]}


def load() -> tuple[list[dict], dict]:
    mult = fee_multipliers()
    markets = [json.loads(line) for line in (CACHE / "markets.jsonl").open()]
    for m in markets:
        m["fee_mult"] = mult.get(m["series"], 1.0)
    candles = {}
    for line in (CACHE / "candles.jsonl").open():
        d = json.loads(line)
        candles[d["ticker"]] = d["candles"]
    return markets, candles


def report():
    markets, candles = load()
    data = rows(markets, candles)
    scored, trials = score(cells(data))
    sched = [m for m in markets if usable(m)]
    print(f"Markets settled with volume >= {MIN_VOLUME}: {len(markets)}; open at an entry time: {len(sched)}; "
          f"with candles: {sum(1 for m in sched if candles.get(m['ticker']))}")
    print(f"Rows (market x horizon x side with a two-sided quote): {len(data)}")
    print(f"Cells defined (trials): {trials}; cells with n >= {MIN_N}: {len(scored)}\n")
    by_cat = {}
    for m in sched:
        by_cat[m["cat"]] = by_cat.get(m["cat"], 0) + 1
    print("Usable markets by category: " + ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items())) + "\n")
    print("adj low: the bootstrap low end at the Bonferroni level, computed only when the 95% low end is "
          "above 0. win low: the exact binomial lower bound on the win rate at that level, against "
          "break-even (entry plus fee).\n")
    print("| cat | h | side | bucket | n | events | price | win | net/contract | 95% CI | adj low | win low / break-even |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in sorted(scored, key=lambda c: -c["lo95"]):
        print(f"| {c['cat']} | {c['h']} | {c['side']} | {c['lo']:.1f}-{c['hi']:.1f} | {c['n']} | {c['events']} | "
              f"{c['price']:.3f} | {c['win']:.3f} | {c['mean']:+.4f} | {c['lo95']:+.4f}..{c['hi95']:+.4f} | "
              f"{c['lo_adj']:+.4f} | {c['win_lo_adj']:.3f} / {c['breakeven']:.3f} |")
    survivors = [c for c in scored if c["survives"]]
    (CACHE / "survivors.json").write_text(json.dumps(survivors, indent=1))
    print(f"\nSurvivors (adjusted bootstrap low end > 0 and adjusted win low > break-even): {len(survivors)}")
    print("Verdict: " + ("GO, see survivors" if survivors else "NO-GO"))


def open_markets():
    """Open markets closing in the next 30h whose current taker price sits in a surviving cell."""
    survivors = json.loads((CACHE / "survivors.json").read_text())
    if not survivors:
        print("No surviving cell: nothing to list.")
        return
    now = int(time.time())
    cursor, hits = None, []
    while True:
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        d = get("/events", **params)
        for e in d.get("events", []):
            for m in e.get("markets") or []:
                left = (ts(m["expected_expiration_time"]) - now) / 3600
                if not 0 < left <= 30 or ts(m["close_time"]) <= now:
                    continue
                bid, ask = float(m["yes_bid_dollars"] or 0), float(m["yes_ask_dollars"] or 0)
                for c in survivors:
                    h_ok = abs(left - c["h"]) <= max(1, c["h"] * 0.25)
                    entry = ask if c["side"] == "yes" else 1 - bid
                    cat_ok = c["cat"] in ("all", category(e["category"]))
                    if cat_ok and h_ok and c["lo"] <= entry < c["hi"] and 0 < bid < ask < 1:
                        hits.append((m["ticker"], m["title"], c["side"], entry, round(left, 1)))
        cursor = d.get("cursor")
        if not cursor or not d.get("events"):
            break
    for h in hits:
        print(h)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["fetch", "report", "open"])
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--reuse", action="store_true", help="fetch: keep the cached market list, fetch only missing candles")
    a = p.parse_args()
    {"fetch": lambda: fetch(a.days, a.reuse), "report": report, "open": open_markets}[a.cmd]()


if __name__ == "__main__":
    main()
