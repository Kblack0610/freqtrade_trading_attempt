#!/usr/bin/env python3
"""The Kalshi order path, on the demo exchange only: mock funds, keys apart from production.

  python -m lab.markets.kalshi_trade balance
  python -m lab.markets.kalshi_trade drill [--ticker T]   # rest one contract, read it back, cancel it, read it back

KalshiDemo refuses any base URL whose host is not the demo's (DEMO_HOSTS), so no config or env can point it at
real money; P9 adds Kalshi to the live path behind its own guard (docs/live.md), never by widening this one.
Requests are signed as docs.kalshi.com getting_started/api_keys states: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP
(epoch ms) and KALSHI-ACCESS-SIGNATURE, the base64 signature of timestamp + method + path (with /trade-api/v2,
without the query), RSA-PSS SHA-256 with a digest-length salt, or Ed25519. Orders go through the V2 event-order
routes: POST /portfolio/events/orders (side bid buys yes; count and price are fixed-point strings, price in
dollars) and DELETE /portfolio/events/orders/{id}; GET /portfolio/orders/{id} reads one back. Reads lag writes on the
demo: a 404 right after the place, the old status right after the cancel (docs/markets.md, Demo orders), so both wait
within `read_polls` x `read_poll_s`. Every request goes through lab/markets/api.py Api with [demo.kalshi]'s pace and
budget, signed afresh on every attempt; a place (POST) is retried on a 429 only, never after a 5xx or a timeout.

The drill (lab/markets/drill.py) picks an open demo market on exchange index `drill_exchange_index`, the shard the
mock funds sit on, with an ask above `drill_price` (or takes --ticker), rests
one yes contract at `drill_price`, post only, then cancels it. It exits 1 unless the order was seen resting and then
canceled.
"""

import argparse
import base64
import os
import sys
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from lab.db import connect
from lab.markets.api import Api, num, when
from lab.markets.drill import await_terminal, rest_and_cancel
from lab.markets.recorder import load_config
from lab.markets.venue import Fill, Position

DEMO_HOSTS = frozenset({"external-api.demo.kalshi.co", "demo-api.kalshi.co"})


class NotDemo(ValueError):
    """A Kalshi trading client pointed anywhere but the demo exchange."""


class NotFound(RuntimeError):
    """HTTP 404 from the demo exchange."""


def signer(pem: bytes):
    """sign(message) -> signature for a Kalshi API key's private key, RSA or Ed25519."""
    key = serialization.load_pem_private_key(pem, password=None)
    if isinstance(key, rsa.RSAPrivateKey):
        pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
        return lambda msg: key.sign(msg, pss, hashes.SHA256())
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.sign
    raise ValueError(f"a Kalshi key is RSA or Ed25519, not {type(key).__name__}")


class KalshiDemo:
    venue = "kalshi_demo"
    resting = frozenset({"resting"})
    terminal = frozenset({"canceled", "executed"})

    def __init__(self, cfg: dict, key_id: str, pem: bytes, session=None, clock=None, sleep=None):
        url = urlparse(cfg["base_url"])
        if url.scheme != "https" or url.hostname not in DEMO_HOSTS:
            raise NotDemo(f"refusing {cfg['base_url']}: the order path runs on the Kalshi demo exchange only")
        self.path = url.path.rstrip("/")
        self.key_id, self.sign = key_id, signer(pem)
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.api = Api(self.venue, cfg, session, self.sleep)
        self.read_polls, self.read_poll_s = int(cfg["read_polls"]), float(cfg["read_poll_s"])
        self.market_pages = int(cfg.get("market_pages", 1))

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(self.clock() * 1000))
        sig = self.sign(f"{ts}{method}{self.path}{path.split('?')[0]}".encode())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        r = self.api.request(method, path, params, body, headers=lambda _: self.headers(method, path))
        if r.status_code >= 400:
            kind = NotFound if r.status_code == 404 else RuntimeError
            raise kind(f"kalshi demo {method} {path}: HTTP {r.status_code}: {str(r.text)[:300]}")
        return r.json()

    def balance(self) -> dict:
        return self.call("GET", "/portfolio/balance")

    def open_markets(self):
        """Pages of open markets, following the cursor for up to market_pages pages."""
        cursor = None
        for _ in range(self.market_pages):
            q = {"status": "open", "mve_filter": "exclude", "limit": 200} | ({"cursor": cursor} if cursor else {})
            page = self.call("GET", "/markets", q)
            yield page.get("markets") or []
            if not (cursor := page.get("cursor")):
                return

    def place(self, ticker: str, price: float, count: int, client_id: str) -> dict:
        return self.call("POST", "/portfolio/events/orders", body={
            "ticker": ticker, "client_order_id": client_id, "side": "bid", "count": f"{count:.2f}",
            "price": f"{price:.4f}", "time_in_force": "good_till_canceled", "self_trade_prevention_type": "maker",
            "post_only": True})

    def order(self, order_id: str) -> dict:
        for _ in range(self.read_polls):
            try:
                return self.call("GET", f"/portfolio/orders/{order_id}")["order"]
            except NotFound:
                self.sleep(self.read_poll_s)
        return self.call("GET", f"/portfolio/orders/{order_id}")["order"]

    def cancel(self, order_id: str, ticker: str) -> dict:
        done = self.call("DELETE", f"/portfolio/events/orders/{order_id}", {"market_ticker": ticker})
        await_terminal(self, order_id, self.sleep, self.read_polls, self.read_poll_s)
        return done

    def positions(self) -> list[Position]:
        """Markets with a position, one page: position_fp is signed, a no position negative."""
        page = self.call("GET", "/portfolio/positions", {"limit": 200, "count_filter": "position"})
        return [Position(p["ticker"], num(p["position_fp"]), num(p.get("market_exposure_dollars")))
                for p in page.get("market_positions") or []]

    def fills(self) -> list[Fill]:
        """Buying yes or selling no adds to the yes count, so those fills are positive."""
        page = self.call("GET", "/portfolio/fills", {"limit": 100})
        return [Fill(f["order_id"], f["ticker"], when(f["created_time"]),
                     num(f["count_fp"]) * (1 if (f["side"] == "yes") == (f["action"] == "buy") else -1),
                     num(f["yes_price_dollars"])) for f in page.get("fills") or []]


def pick(markets: list[dict], price: float, shard: int) -> str | None:
    """The first market on exchange index `shard` whose yes ask sits above `price`, so a post-only bid there rests."""
    for m in markets:
        ask = num(m.get("yes_ask_dollars"))
        if m.get("exchange_index") == shard and ask is not None and price < ask < 1:
            return m["ticker"]
    return None


def drill(conn, client: KalshiDemo, price: float, shard: int, ticker: str | None = None, now=None) -> dict:
    """lab/markets/drill.py on a market of exchange index `shard` that lets a post-only bid at `price` rest. Returns
    the demo_orders row."""
    ticker = ticker or next((t for page in client.open_markets() if (t := pick(page, price, shard))), None)
    if ticker is None:
        raise RuntimeError(f"no open demo market on exchange index {shard} with an ask above {price} "
                           f"in {client.market_pages} pages")
    return rest_and_cancel(conn, client, ticker, price, now)


def from_env(cfg: dict) -> KalshiDemo:
    key_id, path = os.environ.get("KALSHI_DEMO_KEY_ID"), os.environ.get("KALSHI_DEMO_KEY_FILE")
    if not (key_id and path):
        sys.exit("kalshi_trade: need KALSHI_DEMO_KEY_ID and KALSHI_DEMO_KEY_FILE (the demo account's key)")
    with open(path, "rb") as f:
        return KalshiDemo(cfg["demo"]["kalshi"], key_id, f.read())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="markets.kalshi_trade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("balance")
    d = sub.add_parser("drill")
    d.add_argument("--ticker")
    a = ap.parse_args(argv)
    cfg = load_config()
    client = from_env(cfg)
    if a.cmd == "balance":
        print(f"kalshi_trade: demo balance {client.balance()}", flush=True)
        return 0
    with connect(autocommit=True) as conn:
        k = cfg["demo"]["kalshi"]
        row = drill(conn, client, k["drill_price"], int(k["drill_exchange_index"]), a.ticker)
    print(f"kalshi_trade: drill {row}", flush=True)
    return 0 if row["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
