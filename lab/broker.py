#!/usr/bin/env python3
"""Broker accounts over their official API, paper only (docs/platforms.md, API brokers). Today that is Alpaca.

  python -m lab.broker read    # one broker_balances row
  python -m lab.broker drill   # rest one share far below the market, read it back, cancel it, read it back

AlpacaPaper refuses any base URL whose host is not the paper API's, so no config or env can point it at real money. A
live account would come in behind lab/live.py's guard (docs/live.md), never by widening this one. Keys are the paper
account's own, ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY, sent as the APCA-API-KEY-ID and APCA-API-SECRET-KEY
headers. With none set, `read` prints that and exits 0, as lab/wallet.py does with no accounts, so its job can
exist before the keys do.

Errors carry the HTTP status and the broker's message, never a key. `read` exits 1 when the read failed; `drill` exits
1 unless the order was seen resting and then canceled (lab/markets/drill.py, writing to demo_orders).
"""

import argparse
import datetime as dt
import os
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import requests

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


def read(client: AlpacaPaper) -> dict:
    row = {"broker": client.venue, "status": None, "currency": None, "equity": None, "cash": None,
           "buying_power": None, "positions": None, "error": None}
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
    conn.execute("INSERT INTO broker_balances (broker, read_at, status, currency, equity, cash, buying_power, positions, error)"
                 " VALUES (%(broker)s, %(read_at)s, %(status)s, %(currency)s, %(equity)s, %(cash)s, %(buying_power)s,"
                 " %(positions)s, %(error)s)", {**row, "read_at": now})


def main(argv: list[str] | None = None, session=None) -> int:
    ap = argparse.ArgumentParser(prog="lab.broker")
    ap.add_argument("cmd", choices=("read", "drill"))
    a = ap.parse_args(argv)
    key_id, secret = os.environ.get("ALPACA_PAPER_KEY_ID"), os.environ.get("ALPACA_PAPER_SECRET_KEY")
    if not (key_id and secret):
        print("broker: no ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY", flush=True)
        return 0 if a.cmd == "read" else 2
    cfg = tomllib.loads(CONFIG.read_text())["alpaca"]
    client = AlpacaPaper(cfg, key_id, secret, session)
    if a.cmd == "drill":
        with connect(autocommit=True) as conn:
            row = rest_and_cancel(conn, client, cfg["drill_symbol"], cfg["drill_price"])
        print(f"broker: drill {row}", flush=True)
        return 0 if row["error"] is None else 1
    row = read(client)
    with connect(autocommit=True) as conn:
        store(conn, row, dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
    print(f"broker: {row['broker']}: equity {row['equity']} {row['currency']}, {row['positions']} positions, error={row['error']}", flush=True)
    return 1 if row["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
