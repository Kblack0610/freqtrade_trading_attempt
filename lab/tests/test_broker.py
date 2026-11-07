"""The Alpaca paper client: the host guard, the account read, and the drill, against a fake paper API."""

import datetime as dt

import pytest
import requests

from lab import broker
from lab.broker import AlpacaPaper, NotPaper

PAPER = {"base_url": "https://paper-api.alpaca.markets", "timeout_s": 5}
ACCOUNT = {"id": "a1", "status": "ACTIVE", "currency": "USD", "equity": "100012.50", "cash": "99000.00",
           "buying_power": "199000.00"}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        if self.status_code == 204:
            raise ValueError("no body")
        return self.body


class Paper:
    """A paper account with one order, cancelled after `pending` reads of pending_cancel; records every request."""

    def __init__(self, placed="accepted", pending=0, cancel_status=204, account=200):
        self.calls, self.status, self.placed = [], None, placed
        self.pending, self.cancel_status, self.account_status = pending, cancel_status, account

    def request(self, method, url, json, timeout, headers):
        path = url.removeprefix("https://paper-api.alpaca.markets")
        self.calls.append((method, path, json, headers))
        if (method, path) == ("GET", "/v2/account"):
            return Resp(self.account_status, ACCOUNT if self.account_status == 200 else {"message": "forbidden."})
        if (method, path) == ("GET", "/v2/positions"):
            return Resp(200, [{"symbol": "SPY"}, {"symbol": "QQQ"}])
        if (method, path) == ("POST", "/v2/orders"):
            self.status = self.placed
            return Resp(200, {"id": "o1", "status": self.status})
        if (method, path) == ("GET", "/v2/orders/o1"):
            if self.status == "pending_cancel":
                if self.pending == 0:
                    self.status = "canceled"
                self.pending -= 1
            return Resp(200, {"id": "o1", "status": self.status})
        if (method, path) == ("DELETE", "/v2/orders/o1"):
            if self.cancel_status == 204:
                self.status = "pending_cancel"
            return Resp(self.cancel_status, None if self.cancel_status == 204 else {"message": "busy"})
        return Resp(404, {"message": path})


def client(s, slept=None):
    return AlpacaPaper(PAPER, "key-1", "secret-1", s, (slept if slept is not None else []).append)


@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "http://paper-api.alpaca.markets",
                                 "https://paper-api.alpaca.markets.evil.example", "https://broker-api.alpaca.markets"])
def test_anything_but_the_paper_api_is_refused(url):
    with pytest.raises(NotPaper):
        AlpacaPaper({"base_url": url}, "k", "s")


def test_the_shipped_config_points_at_paper():
    cfg = broker.tomllib.loads(broker.CONFIG.read_text())["alpaca"]
    AlpacaPaper(cfg, "k", "s")  # does not raise


def test_a_read_carries_the_keys_in_headers_and_fills_the_row():
    s = Paper()
    row = broker.read(client(s))
    assert row == {"broker": "alpaca_paper", "status": "ACTIVE", "currency": "USD", "equity": 100012.5, "cash": 99000.0,
                   "buying_power": 199000.0, "positions": 2, "error": None}
    assert s.calls[0][3] == {"APCA-API-KEY-ID": "key-1", "APCA-API-SECRET-KEY": "secret-1"}


def test_a_refused_read_is_a_row_with_the_status_and_never_the_key():
    row = broker.read(client(Paper(account=403)))
    assert row["equity"] is None and "HTTP 403" in row["error"]
    assert "secret-1" not in row["error"] and "key-1" not in row["error"]


def test_a_network_failure_is_a_row_too():
    class Down:
        def request(self, *a, **k):
            raise requests.ConnectionError("no route to paper-api")
    assert broker.read(client(Down()))["error"] == "alpaca paper: ConnectionError"


def test_a_row_is_stored(conn):
    now = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
    broker.store(conn, broker.read(client(Paper())), now)
    assert conn.execute("SELECT broker, read_at, equity, positions FROM broker_balances").fetchall() == \
        [("alpaca_paper", now, 100012.5, 2)]


def test_the_drill_rests_one_share_then_cancels_it(conn):
    s, slept = Paper(pending=2), []
    row = broker.rest_and_cancel(conn, client(s, slept), "SPY", 1.0)
    assert (row["ticker"], row["order_id"], row["status"], row["error"]) == ("SPY", "o1", "canceled", None)
    assert row["canceled_at"] is not None and len(slept) == 2
    post = next(c for c in s.calls if c[0] == "POST")[2]
    assert {k: post[k] for k in ("symbol", "qty", "side", "type", "time_in_force", "limit_price")} == \
        {"symbol": "SPY", "qty": "1", "side": "buy", "type": "limit", "time_in_force": "day", "limit_price": "1.00"}
    assert post["client_order_id"]


def test_an_order_that_filled_fails_the_drill(conn):
    s = Paper(placed="filled")
    row = broker.rest_and_cancel(conn, client(s), "SPY", 1.0)
    assert row["status"] == "filled" and "not resting" in row["error"]
    assert not any(c[0] == "DELETE" for c in s.calls)


def test_a_failed_cancel_is_recorded_and_the_order_is_placed_once(conn):
    s = Paper(cancel_status=503)
    row = broker.rest_and_cancel(conn, client(s), "SPY", 1.0)
    assert row["status"] == "accepted" and row["canceled_at"] is None and "HTTP 503" in row["error"]
    assert [c[0] for c in s.calls].count("POST") == 1


def test_a_cancel_stuck_pending_fails_the_drill_within_its_bound(conn):
    s, slept = Paper(pending=99), []
    row = broker.rest_and_cancel(conn, client(s, slept), "SPY", 1.0)
    assert row["status"] == "pending_cancel" and "after the cancel" in row["error"]
    assert len(slept) == broker.CANCEL_POLLS


def test_main_reads_and_stores_with_keys_set(conn, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key-1")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret-1")
    assert broker.main(["read"], session=Paper()) == 0
    assert conn.execute("SELECT equity, error FROM broker_balances").fetchall() == [(100012.5, None)]


def test_main_exits_1_when_the_read_failed(conn, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key-1")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret-1")
    assert broker.main(["read"], session=Paper(account=403)) == 1
    assert "HTTP 403" in conn.execute("SELECT error FROM broker_balances").fetchone()[0]


def test_no_keys_is_a_quiet_read_and_a_refused_drill(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)
    assert broker.main(["read"]) == 0
    assert broker.main(["drill"]) == 2
