#!/usr/bin/env python3
"""The Kalshi order path, on the demo exchange only: mock funds, keys apart from production.

  python -m markets.kalshi_trade balance
  python -m markets.kalshi_trade drill [--ticker T]   # rest one contract, read it back, cancel it, read it back

KalshiDemo refuses any base URL whose host is not the demo's (DEMO_HOSTS), so no config or env can point it at
real money; P9 adds Kalshi to the live path behind its own guard (docs/live.md), never by widening this one.
Requests are signed as docs.kalshi.com getting_started/api_keys states: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP
(epoch ms) and KALSHI-ACCESS-SIGNATURE, the base64 signature of timestamp + method + path (with /trade-api/v2,
without the query), RSA-PSS SHA-256 with a digest-length salt, or Ed25519. Orders go through the V2 event-order
routes: POST /portfolio/events/orders (side bid buys yes; count and price are fixed-point strings, price in
dollars) and DELETE /portfolio/events/orders/{id}; GET /portfolio/orders/{id} reads one back.

The drill is the proof that the order path works end to end. It picks an open demo market with an ask above
`drill_price` (or takes --ticker), rests one yes contract at `drill_price`, post only, then cancels it, and writes
each step to demo_orders (migration 023). It exits 1 unless the order was seen resting and then canceled. A POST is
never retried: a timeout could have placed it, and a second one would double it.
"""

import argparse
import base64
import datetime as dt
import os
import sys
import time
import uuid
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from db import connect
from markets.api import num
from markets.recorder import load_config

DEMO_HOSTS = frozenset({"external-api.demo.kalshi.co", "demo-api.kalshi.co"})
UTC = dt.timezone.utc


class NotDemo(ValueError):
    """A Kalshi trading client pointed anywhere but the demo exchange."""


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

    def __init__(self, cfg: dict, key_id: str, pem: bytes, session=None, clock=None):
        url = urlparse(cfg["base_url"])
        if url.scheme != "https" or url.hostname not in DEMO_HOSTS:
            raise NotDemo(f"refusing {cfg['base_url']}: the order path runs on the Kalshi demo exchange only")
        self.base, self.path = cfg["base_url"].rstrip("/"), url.path.rstrip("/")
        self.key_id, self.sign = key_id, signer(pem)
        self.timeout = float(cfg.get("timeout_s", 30))
        self.http = session or requests.Session()
        self.clock = clock or time.time

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(self.clock() * 1000))
        sig = self.sign(f"{ts}{method}{self.path}{path.split('?')[0]}".encode())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        r = self.http.request(method, f"{self.base}{path}", params=params, json=body, timeout=self.timeout,
                              headers=self.headers(method, path))
        if r.status_code >= 400:
            raise RuntimeError(f"kalshi demo {method} {path}: HTTP {r.status_code}: {str(r.text)[:300]}")
        return r.json()

    def balance(self) -> dict:
        return self.call("GET", "/portfolio/balance")

    def open_markets(self) -> list[dict]:
        return self.call("GET", "/markets", {"status": "open", "mve_filter": "exclude", "limit": 200}).get("markets") or []

    def place(self, ticker: str, price: float, count: int, client_id: str) -> dict:
        return self.call("POST", "/portfolio/events/orders", body={
            "ticker": ticker, "client_order_id": client_id, "side": "bid", "count": f"{count:.2f}",
            "price": f"{price:.4f}", "time_in_force": "good_till_canceled", "self_trade_prevention_type": "maker",
            "post_only": True})

    def order(self, order_id: str) -> dict:
        return self.call("GET", f"/portfolio/orders/{order_id}")["order"]

    def cancel(self, order_id: str, ticker: str) -> dict:
        return self.call("DELETE", f"/portfolio/events/orders/{order_id}", {"market_ticker": ticker})


def pick(markets: list[dict], price: float) -> str | None:
    """The first market whose yes ask sits above `price`, so a post-only bid there rests."""
    for m in markets:
        ask = num(m.get("yes_ask_dollars"))
        if ask is not None and price < ask < 1:
            return m["ticker"]
    return None


def drill(conn, client: KalshiDemo, price: float, ticker: str | None = None, now=None) -> dict:
    """Rest, read, cancel, read (module docstring). Returns the demo_orders row; its error is None on success."""
    now = now or (lambda: dt.datetime.now(UTC))
    ticker = ticker or pick(client.open_markets(), price)
    if ticker is None:
        raise RuntimeError(f"no open demo market with an ask above {price}")
    cid = str(uuid.uuid4())
    rid = conn.execute("INSERT INTO demo_orders (venue, ticker, client_id, price, count, placed_at) "
                       "VALUES (%s, %s, %s, %s, 1, %s) RETURNING id", (client.venue, ticker, cid, price, now())).fetchone()[0]

    def note(**kv):
        conn.execute(f"UPDATE demo_orders SET {', '.join(f'{k} = %s' for k in kv)} WHERE id = %s", (*kv.values(), rid))

    try:
        oid = client.place(ticker, price, 1, cid)["order_id"]
        note(order_id=oid)
        status = client.order(oid)["status"]
        note(status=status)
        if status != "resting":
            raise RuntimeError(f"order {oid} is {status}, not resting")
        client.cancel(oid, ticker)
        status = client.order(oid)["status"]
        note(status=status, canceled_at=now() if status == "canceled" else None)
        if status != "canceled":
            raise RuntimeError(f"order {oid} is {status} after the cancel")
    except Exception as e:  # noqa: BLE001 - the row carries what went wrong, and the exit code says it failed
        note(error=f"{type(e).__name__}: {e}"[:1000])
    return dict(zip(("id", "ticker", "order_id", "status", "canceled_at", "error"), conn.execute(
        "SELECT id, ticker, order_id, status, canceled_at, error FROM demo_orders WHERE id = %s", (rid,)).fetchone()))


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
        row = drill(conn, client, cfg["demo"]["kalshi"]["drill_price"], a.ticker)
    print(f"kalshi_trade: drill {row}", flush=True)
    return 0 if row["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
