#!/usr/bin/env python3
"""Broker accounts over their official API: paper accounts, and read-only reads of real ones (docs/platforms.md).

  python -m lab.broker read    # one broker_balances row per account, for every source whose keys are set
  python -m lab.broker drill   # Alpaca paper: rest one share far below the market, read it back, cancel it, read it back

Sources, each with its own env:
- Alpaca paper (ALPACA_PAPER_KEY_ID, ALPACA_PAPER_SECRET_KEY). AlpacaPaper refuses any base URL whose host is not the
  paper API's, so no config or env can point it at real money. It is the only client here that can place an order.
- Robinhood Crypto (ROBINHOOD_API_KEY, ROBINHOOD_PRIVATE_KEY: the base64 Ed25519 seed). A real account, read only.
- SnapTrade (SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY: a Personal API key). Reads the accounts the person
  connected with a read connection, Fidelity first. A real account, read only.

The two real-account clients have no order method and send only GET, so a key that could trade still cannot from
here. A live order would come in behind lab/live.py's guard (docs/live.md), never by widening these. A source with no
keys is skipped; with none set, `read` prints that and exits 0, as lab/wallet.py does with no accounts, so its job can
exist before the keys do.

Errors carry the HTTP status and the broker's message, never a key. `read` exits 1 when any read failed; `drill` exits
1 unless the order was seen resting and then canceled (lab/markets/drill.py, writing to demo_orders).
"""

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab.db import connect
from lab.markets.api import num
from lab.markets.drill import rest_and_cancel

CONFIG = Path(os.environ.get("BROKER_CONFIG", Path(__file__).resolve().parent / "broker.toml"))
PAPER_HOST = "paper-api.alpaca.markets"
# A cancel is asynchronous and only the trade-updates websocket reports its end, too much for a one-shot drill, so
# the order is re-read until it leaves pending_cancel, within this bound.
CANCEL_POLL_S, CANCEL_POLLS = 1.0, 10


class NotPaper(ValueError):
    """An Alpaca client pointed anywhere but the paper API."""


class WrongHost(ValueError):
    """A read-only client pointed anywhere but its platform's API."""


def https_host(cfg: dict, host: str) -> str:
    url = urlparse(cfg["base_url"])
    if url.scheme != "https" or url.hostname != host:
        raise WrongHost(f"refusing {cfg['base_url']}: this client reads {host} only")
    return cfg["base_url"].rstrip("/")


def blank_row(broker_name: str, paper: bool) -> dict:
    return {"broker": broker_name, "paper": paper, "status": None, "currency": None, "equity": None, "cash": None,
            "buying_power": None, "positions": None, "error": None}


class AlpacaPaper:
    venue = "alpaca_paper"
    resting = frozenset({"pending_new", "accepted", "new"})

    def __init__(self, cfg: dict, key_id: str, secret: str, session=None, sleep=None):
        url = urlparse(cfg["base_url"])
        if url.scheme != "https" or url.hostname != PAPER_HOST:
            raise NotPaper(f"refusing {cfg['base_url']}: this client runs on the Alpaca paper API only")
        self.base = cfg["base_url"].rstrip("/")
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.timeout = float(cfg["timeout_s"])
        self.http = session or requests.Session()
        self.sleep = sleep or time.sleep

    def call(self, method: str, path: str, body: dict | None = None):
        r = self.http.request(method, f"{self.base}{path}", json=body, timeout=self.timeout, headers=self.headers)
        if r.status_code >= 400:
            raise RuntimeError(f"alpaca paper {method} {path}: HTTP {r.status_code}: {str(r.text)[:300]}")
        return r.json() if r.status_code != 204 else None

    def account(self) -> dict:
        return self.call("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self.call("GET", "/v2/positions")

    def place(self, ticker: str, price: float, count: int, client_id: str) -> dict:
        o = self.call("POST", "/v2/orders", {
            "symbol": ticker, "qty": str(count), "side": "buy", "type": "limit", "time_in_force": "day",
            "limit_price": f"{price:.2f}", "client_order_id": client_id})
        return {"order_id": o["id"]}

    def order(self, order_id: str) -> dict:
        return self.call("GET", f"/v2/orders/{order_id}")

    def cancel(self, order_id: str, ticker: str) -> None:
        self.call("DELETE", f"/v2/orders/{order_id}")
        for _ in range(CANCEL_POLLS):
            if self.order(order_id)["status"] != "pending_cancel":
                return
            self.sleep(CANCEL_POLL_S)


class RobinhoodCrypto:
    """The Robinhood Crypto Trading API, GET only. Each request signs api_key + timestamp + path(+query) + method + body
    with the Ed25519 key whose public half the person registered; a timestamp is good for 30 s."""

    venue = "robinhood_crypto"
    host = "trading.robinhood.com"

    def __init__(self, cfg: dict, api_key: str, private_key_b64: str, session=None, clock=None):
        self.base = https_host(cfg, self.host)
        self.api_key = api_key
        self.key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64)[:32])
        self.quote = cfg["quote"]
        self.timeout = float(cfg["timeout_s"])
        self.http = session or requests.Session()
        self.clock = clock or time.time

    def get(self, path: str):
        ts = str(int(self.clock()))
        sig = self.key.sign(f"{self.api_key}{ts}{path}GET".encode())
        headers = {"x-api-key": self.api_key, "x-timestamp": ts, "x-signature": base64.b64encode(sig).decode()}
        r = self.http.request("GET", f"{self.base}{path}", json=None, timeout=self.timeout, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"robinhood GET {path.split('?')[0]}: HTTP {r.status_code}: {str(r.text)[:300]}")
        return r.json()

    def holdings(self) -> list[dict]:
        out, path = [], "/api/v1/crypto/trading/holdings/"
        while path:
            page = self.get(path)
            out += page["results"]
            nxt = urlparse(page.get("next") or "")
            if nxt.path and nxt.hostname != self.host:
                raise RuntimeError(f"robinhood: next page on {nxt.hostname}, not {self.host}")
            path = nxt.path + (f"?{nxt.query}" if nxt.query else "") if nxt.path else None
        return out

    def prices(self, assets: list[str]) -> dict[str, float]:
        if not assets:
            return {}
        q = "&".join(f"symbol={a}-{self.quote}" for a in assets)
        return {p["symbol"]: num(p["price"]) for p in self.get(f"/api/v1/crypto/marketdata/best_bid_ask/?{q}")["results"]}

    def read(self) -> list[dict]:
        """One row: cash is buying power, equity adds each holding at the best bid/ask midpoint."""
        row = blank_row(self.venue, False)
        try:
            a = self.get("/api/v1/crypto/trading/accounts/")
            held = {h["asset_code"]: num(h["total_quantity"]) for h in self.holdings() if num(h["total_quantity"])}
            px = self.prices(sorted(held))
            row.update(status=a["status"], currency=a["buying_power_currency"], cash=num(a["buying_power"]),
                       buying_power=num(a["buying_power"]), positions=len(held))
            missing = [k for k in sorted(held) if f"{k}-{self.quote}" not in px]
            if missing:
                row["error"] = f"robinhood: no {self.quote} price for {', '.join(missing)}"
            else:
                row["equity"] = row["cash"] + sum(q * px[f"{k}-{self.quote}"] for k, q in held.items())
        except RuntimeError as e:
            row["error"] = str(e)
        except KeyError as e:
            row["error"] = f"robinhood: no field {e}"
        except requests.RequestException as e:
            row["error"] = f"robinhood: {type(e).__name__}"
        return [row]


class SnapTrade:
    """SnapTrade with a Personal API key, GET only: the accounts the person connected (Fidelity) through a read
    connection. Each request carries clientId and timestamp in the query and a Signature header, the base64
    HMAC-SHA256 under the consumer key of the canonical JSON {content, path, query}. Its data refreshes once a day."""

    host = "api.snaptrade.com"

    def __init__(self, cfg: dict, client_id: str, consumer_key: str, session=None, clock=None):
        self.base = https_host(cfg, self.host)
        self.client_id, self.consumer_key = client_id, consumer_key
        self.timeout = float(cfg["timeout_s"])
        self.http = session or requests.Session()
        self.clock = clock or time.time

    def get(self, path: str):
        query = f"clientId={self.client_id}&timestamp={int(self.clock())}"
        signed = json.dumps({"content": None, "path": path, "query": query}, separators=(",", ":"), sort_keys=True)
        sig = base64.b64encode(hmac.new(self.consumer_key.encode(), signed.encode(), hashlib.sha256).digest()).decode()
        r = self.http.request("GET", f"{self.base}{path}?{query}", json=None, timeout=self.timeout,
                              headers={"Signature": sig})
        if r.status_code >= 400:
            raise RuntimeError(f"snaptrade GET {path}: HTTP {r.status_code}: {str(r.text)[:300]}")
        return r.json()

    @staticmethod
    def name(a: dict) -> str:
        """institution_account-name_last4, lower case, so two accounts at one broker stay apart."""
        raw = f"{a['institution_name']} {a.get('name') or ''} {str(a.get('number') or '')[-4:]}"
        return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")

    def read(self) -> list[dict]:
        """One row per connected account, its total value as SnapTrade last synced it."""
        try:
            accounts = self.get("/accounts")
        except RuntimeError as e:
            return [{**blank_row("snaptrade", False), "error": str(e)}]
        except requests.RequestException as e:
            return [{**blank_row("snaptrade", False), "error": f"snaptrade: {type(e).__name__}"}]
        rows = []
        for a in accounts:
            row = blank_row(self.name(a), bool(a.get("is_paper")))
            total = (a.get("balance") or {}).get("total")
            row["status"] = a.get("status")
            if total:
                row.update(currency=total["currency"], equity=num(total["amount"]))
            else:
                row["error"] = "snaptrade: no total balance for this account"
            rows.append(row)
        return rows


def read(client: AlpacaPaper) -> dict:
    row = blank_row(client.venue, True)
    try:
        a = client.account()
        row.update(status=a["status"], currency=a["currency"], equity=num(a["equity"]), cash=num(a["cash"]),
                   buying_power=num(a["buying_power"]), positions=len(client.positions()))
    except RuntimeError as e:
        row["error"] = str(e)
    except KeyError as e:
        row["error"] = f"alpaca paper: no field {e}"
    except requests.RequestException as e:
        row["error"] = f"alpaca paper: {type(e).__name__}"
    return row


def store(conn, row: dict, now: dt.datetime) -> None:
    conn.execute("INSERT INTO broker_balances (broker, read_at, paper, status, currency, equity, cash, buying_power,"
                 " positions, error) VALUES (%(broker)s, %(read_at)s, %(paper)s, %(status)s, %(currency)s, %(equity)s,"
                 " %(cash)s, %(buying_power)s, %(positions)s, %(error)s)", {**row, "read_at": now})


def sources(session=None) -> list:
    """Every read source whose keys are set, as a callable returning rows."""
    cfg = tomllib.loads(CONFIG.read_text())
    env = os.environ.get
    out = []
    if env("ALPACA_PAPER_KEY_ID") and env("ALPACA_PAPER_SECRET_KEY"):
        c = AlpacaPaper(cfg["alpaca"], env("ALPACA_PAPER_KEY_ID"), env("ALPACA_PAPER_SECRET_KEY"), session)
        out.append(lambda: [read(c)])
    if env("ROBINHOOD_API_KEY") and env("ROBINHOOD_PRIVATE_KEY"):
        out.append(RobinhoodCrypto(cfg["robinhood"], env("ROBINHOOD_API_KEY"), env("ROBINHOOD_PRIVATE_KEY"), session).read)
    if env("SNAPTRADE_CLIENT_ID") and env("SNAPTRADE_CONSUMER_KEY"):
        out.append(SnapTrade(cfg["snaptrade"], env("SNAPTRADE_CLIENT_ID"), env("SNAPTRADE_CONSUMER_KEY"), session).read)
    return out


def main(argv: list[str] | None = None, session=None) -> int:
    ap = argparse.ArgumentParser(prog="lab.broker")
    ap.add_argument("cmd", choices=("read", "drill"))
    a = ap.parse_args(argv)
    if a.cmd == "drill":
        key_id, secret = os.environ.get("ALPACA_PAPER_KEY_ID"), os.environ.get("ALPACA_PAPER_SECRET_KEY")
        if not (key_id and secret):
            print("broker: no ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY", flush=True)
            return 2
        cfg = tomllib.loads(CONFIG.read_text())["alpaca"]
        with connect(autocommit=True) as conn:
            row = rest_and_cancel(conn, AlpacaPaper(cfg, key_id, secret, session), cfg["drill_symbol"], cfg["drill_price"])
        print(f"broker: drill {row}", flush=True)
        return 0 if row["error"] is None else 1
    found = sources(session)
    if not found:
        print("broker: no broker keys set (Alpaca paper, Robinhood, SnapTrade)", flush=True)
        return 0
    rows = [r for src in found for r in src()]
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    with connect(autocommit=True) as conn:
        for row in rows:
            store(conn, row, now)
    for row in rows:
        print(f"broker: {row['broker']}: equity {row['equity']} {row['currency']}, {row['positions']} positions, error={row['error']}", flush=True)
    return 1 if any(r["error"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
