#!/usr/bin/env python3
"""The Manifold order path: play money (mana) only, so an agent may trade it end to end (docs/platforms.md, Manifold).

  python -m lab.markets.manifold_trade balance
  python -m lab.markets.manifold_trade drill [--ticker MARKET_ID]   # rest one limit bet, read it back, cancel it, read it back

ManifoldBets refuses any base URL but https://api.manifold.markets (MANIFOLD_HOSTS), so the key goes nowhere else.
Requests carry `Authorization: Key <MANIFOLD_API_KEY>` (one key per account). Routes, as docs.manifold.markets/api
states them (2026-11-06): POST /v0/bet with `limitProb` (0.01 to 0.99, whole percents) is a limit order, and
`expiresMillisAfter` has Manifold cancel it on its own, our dead-man switch; POST /v0/bet/cancel/{id} cancels;
GET /v0/bets?userId=&contractId= reads it back (there is no one-bet route); GET /v0/me is the account and its balance.
Every request goes through lab/markets/api.py Api with [demo.manifold]'s pace, on the Manifold reader's budget
([venues.manifold] budget_rps): the limit is per IP, so the drill and the recorder share it. A bet or a cancel (both
POST) is retried on a 429 only, never after a 5xx or a timeout.

The drill (lab/markets/drill.py) picks an open binary mana market whose probability is at least `min_prob` (or takes
--ticker), rests a `drill_mana` YES limit bet at `drill_price`, far below the market so nothing fills, then cancels it.
It exits 1 unless the bet was seen open with nothing filled and then canceled.
"""

import argparse
import os
import sys
import time
from urllib.parse import urlparse

from lab.db import connect
from lab.markets.api import Api, num, when
from lab.markets.budget import Budget
from lab.markets.drill import await_terminal, rest_and_cancel
from lab.markets.recorder import load_config
from lab.markets.venue import Fill, Position

MANIFOLD_HOSTS = frozenset({"api.manifold.markets"})


class NotManifold(ValueError):
    """A Manifold trading client pointed anywhere but Manifold's API."""


def status(bet: dict) -> str:
    """The drill's word for a limit bet: open (resting, nothing filled), partial, filled or canceled."""
    if bet.get("isCancelled"):
        return "canceled"
    if bet.get("isFilled"):
        return "filled"
    return "partial" if (num(bet.get("amount")) or 0) > 0 else "open"


class ManifoldBets:
    venue = "manifold"
    resting = frozenset({"open"})
    terminal = frozenset({"canceled", "filled"})

    def __init__(self, cfg: dict, key: str, session=None, sleep=None, budget: Budget | None = None):
        url = urlparse(cfg["base_url"])
        if url.scheme != "https" or url.hostname not in MANIFOLD_HOSTS:
            raise NotManifold(f"refusing {cfg['base_url']}: the Manifold key goes to api.manifold.markets only")
        self.key = key
        self.mana = float(cfg.get("drill_mana", 1))
        self.expires_ms = int(float(cfg.get("expires_s", 600)) * 1000)
        self.sleep = sleep or time.sleep
        self.api = Api(self.venue, cfg, session, self.sleep, budget)
        self.user_id, self.contracts = None, {}

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        r = self.api.request(method, path, params, body, headers={"Authorization": f"Key {self.key}"})
        if r.status_code >= 400:
            raise RuntimeError(f"manifold {method} {path}: HTTP {r.status_code}: {str(r.text)[:300]}")
        return r.json()

    def me(self) -> dict:
        user = self.call("GET", "/v0/me")
        self.user_id = user["id"]
        return user

    def balance(self) -> dict:
        user = self.me()
        return {"username": user.get("username"), "balance": user.get("balance")}

    def open_markets(self) -> list[dict]:
        return self.call("GET", "/v0/search-markets", {"contractType": "BINARY", "filter": "open", "sort": "newest",
                                                       "limit": 200})

    def place(self, ticker: str, price: float, count: int, client_id: str) -> dict:
        """`count` lots of `drill_mana`. Manifold takes no client id, so client_id stays ours (demo_orders)."""
        bet = self.call("POST", "/v0/bet", body={
            "contractId": ticker, "outcome": "YES", "amount": count * self.mana, "limitProb": round(price, 2),
            "expiresMillisAfter": self.expires_ms})
        self.contracts[bet["betId"]] = ticker
        return {"order_id": bet["betId"]}

    def order(self, order_id: str) -> dict:
        if self.user_id is None:
            self.me()
        bets = self.call("GET", "/v0/bets", {"userId": self.user_id, "contractId": self.contracts[order_id],
                                             "limit": 100})
        bet = next((b for b in bets if b.get("id") == order_id), None)
        if bet is None:
            raise RuntimeError(f"bet {order_id} is not in the account's bets on {self.contracts[order_id]}")
        return {"status": status(bet)}

    def cancel(self, order_id: str, ticker: str) -> None:
        """The bets list lags the cancel: read right after, the drill's bet still said open (2026-11-12)."""
        self.call("POST", f"/v0/bet/cancel/{order_id}")
        await_terminal(self, order_id, self.sleep)

    def bets(self) -> list[dict]:
        """The account's newest bets, one page. A bet's shares and amount are what filled; a sale's are negative."""
        if self.user_id is None:
            self.me()
        return self.call("GET", "/v0/bets", {"userId": self.user_id, "limit": 1000})

    def positions(self) -> list[Position]:
        """Shares held per market, summed over the newest bets: yes positive, no negative; cost is mana spent."""
        held: dict[str, list[float]] = {}
        for b in self.bets():
            sign = 1 if b["outcome"] == "YES" else -1
            h = held.setdefault(b["contractId"], [0.0, 0.0])
            h[0] += sign * (num(b.get("shares")) or 0.0)
            h[1] += num(b.get("amount")) or 0.0
        return [Position(t, n, cost) for t, (n, cost) in held.items() if n]

    def fills(self) -> list[Fill]:
        """Each match of each bet. A share pays one mana, so a no share's price is one less the yes price."""
        out = []
        for b in self.bets():
            yes = b["outcome"] == "YES"
            for f in b.get("fills") or []:
                shares, amount = num(f["shares"]), num(f["amount"])
                if shares:
                    each = amount / shares
                    out.append(Fill(b["id"], b["contractId"], when(f["timestamp"]), shares if yes else -shares,
                                    each if yes else 1 - each))
        return sorted(out, key=lambda f: f.ts, reverse=True)


def pick(markets: list[dict], min_prob: float) -> str | None:
    """The first open, unresolved mana market priced at `min_prob` or more, where a 1% YES limit cannot fill."""
    for m in markets:
        p = num(m.get("probability"))
        if (m.get("mechanism") == "cpmm-1" and m.get("token", "MANA") == "MANA" and not m.get("isResolved")
                and p is not None and min_prob <= p < 1):
            return m["id"]
    return None


def drill(conn, client: ManifoldBets, price: float, min_prob: float, ticker: str | None = None, now=None) -> dict:
    """lab/markets/drill.py on a market where a limit bet at `price` rests. Returns the demo_orders row."""
    ticker = ticker or pick(client.open_markets(), min_prob)
    if ticker is None:
        raise RuntimeError(f"no open binary mana market at {min_prob} or more")
    return rest_and_cancel(conn, client, ticker, price, now)


def from_env(cfg: dict) -> ManifoldBets:
    key = os.environ.get("MANIFOLD_API_KEY")
    if not key:
        sys.exit("manifold_trade: need MANIFOLD_API_KEY (the account's key, rbw trading/manifold)")
    return ManifoldBets(cfg["demo"]["manifold"], key, budget=Budget("manifold", cfg["venues"]["manifold"]["budget_rps"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="markets.manifold_trade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("balance")
    d = sub.add_parser("drill")
    d.add_argument("--ticker")
    a = ap.parse_args(argv)
    cfg = load_config()
    client = from_env(cfg)
    if a.cmd == "balance":
        print(f"manifold_trade: balance {client.balance()}", flush=True)
        return 0
    m = cfg["demo"]["manifold"]
    with connect(autocommit=True) as conn:
        row = drill(conn, client, m["drill_price"], m["min_prob"], a.ticker)
    print(f"manifold_trade: drill {row}", flush=True)
    return 0 if row["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
