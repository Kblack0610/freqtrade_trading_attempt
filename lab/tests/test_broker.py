"""The Alpaca paper client: the host guard, the account read, and the drill, against a fake paper API."""

import base64
import datetime as dt
import hashlib
import hmac

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab import broker
from lab.broker import AlpacaPaper, NotPaper, RobinhoodCrypto, SnapTrade, WrongHost

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
    assert row == {"broker": "alpaca_paper", "paper": True, "status": "ACTIVE", "currency": "USD", "equity": 100012.5, "cash": 99000.0,
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


KEYS = ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY", "ROBINHOOD_API_KEY", "ROBINHOOD_PRIVATE_KEY",
        "SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_no_keys_is_a_quiet_read_and_a_refused_drill():
    assert broker.main(["read"]) == 0
    assert broker.main(["drill"]) == 2


# Robinhood Crypto and SnapTrade: real accounts, read only.

SEED = bytes(range(32))
RH = {"base_url": "https://trading.robinhood.com", "timeout_s": 5, "quote": "USD"}
ST = {"base_url": "https://api.snaptrade.com", "timeout_s": 5}


class Robinhood:
    """A crypto account holding BTC over two holdings pages, and ETH at zero; records every request."""

    def __init__(self, account=200, prices=("BTC-USD",)):
        self.calls, self.account_status, self.priced = [], account, prices

    def request(self, method, url, json, timeout, headers):
        path = url.removeprefix("https://trading.robinhood.com")
        self.calls.append((method, path, json, headers))
        if path == "/api/v1/crypto/trading/accounts/":
            body = {"account_number": "RH1", "status": "active", "buying_power": "250.50", "buying_power_currency": "USD"}
            return Resp(self.account_status, body if self.account_status == 200 else {"errors": [{"detail": "denied"}]})
        if path == "/api/v1/crypto/trading/holdings/":
            return Resp(200, {"next": "https://trading.robinhood.com/api/v1/crypto/trading/holdings/?cursor=c2",
                              "results": [{"asset_code": "BTC", "total_quantity": "0.01"}]})
        if path == "/api/v1/crypto/trading/holdings/?cursor=c2":
            return Resp(200, {"next": None, "results": [{"asset_code": "ETH", "total_quantity": "0"}]})
        if path == "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD":
            return Resp(200, {"results": [{"symbol": s, "price": 60000.0} for s in self.priced]})
        return Resp(404, {"errors": [{"detail": path}]})


def rh(s):
    return RobinhoodCrypto(RH, "rh-key", base64.b64encode(SEED).decode(), s, clock=lambda: 1700000000)


@pytest.mark.parametrize("cls,cfg", [(RobinhoodCrypto, {"base_url": "https://trading.robinhood.com.evil.example"}),
                                     (RobinhoodCrypto, {"base_url": "http://trading.robinhood.com"}),
                                     (SnapTrade, {"base_url": "https://api.snaptrade.example"})])
def test_a_read_only_client_refuses_any_other_host(cls, cfg):
    with pytest.raises(WrongHost):
        cls(cfg, "k", base64.b64encode(SEED).decode())


def test_the_shipped_config_points_at_each_platform():
    cfg = broker.tomllib.loads(broker.CONFIG.read_text())
    RobinhoodCrypto(cfg["robinhood"], "k", base64.b64encode(SEED).decode())
    SnapTrade(cfg["snaptrade"], "c", "k")


def test_the_read_only_clients_have_no_order_method():
    for cls in (RobinhoodCrypto, SnapTrade):
        assert not any(hasattr(cls, m) for m in ("place", "cancel", "post", "call"))


def test_robinhood_signs_key_timestamp_path_and_method_with_the_ed25519_key():
    s = Robinhood()
    rh(s).read()
    method, path, _, h = s.calls[0]
    assert (method, path, h["x-api-key"], h["x-timestamp"]) == ("GET", "/api/v1/crypto/trading/accounts/", "rh-key", "1700000000")
    Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(
        base64.b64decode(h["x-signature"]), b"rh-key1700000000/api/v1/crypto/trading/accounts/GET")
    paged = next(c for c in s.calls if "cursor" in c[1])[3]
    Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(
        base64.b64decode(paged["x-signature"]), b"rh-key1700000000/api/v1/crypto/trading/holdings/?cursor=c2GET")
    assert {c[0] for c in s.calls} == {"GET"}


def test_robinhood_values_holdings_at_the_midpoint_over_every_page():
    [row] = rh(Robinhood()).read()
    assert row == {"broker": "robinhood_crypto", "paper": False, "status": "active", "currency": "USD",
                   "equity": 250.5 + 0.01 * 60000, "cash": 250.5, "buying_power": 250.5, "positions": 1, "error": None}


def test_robinhood_with_an_unpriced_holding_says_so_and_leaves_equity_empty():
    [row] = rh(Robinhood(prices=())).read()
    assert row["equity"] is None and row["cash"] == 250.5 and row["error"] == "robinhood: no USD price for BTC"


def test_a_refused_robinhood_read_is_a_row_without_the_key():
    [row] = rh(Robinhood(account=401)).read()
    assert "HTTP 401" in row["error"] and "rh-key" not in row["error"] and row["equity"] is None


def test_a_robinhood_next_page_on_another_host_is_refused():
    class Elsewhere(Robinhood):
        def request(self, method, url, json, timeout, headers):
            r = super().request(method, url, json, timeout, headers)
            if url.endswith("/holdings/"):
                r.body = {**r.body, "next": "https://evil.example/api/v1/crypto/trading/holdings/?cursor=c2"}
            return r
    [row] = rh(Elsewhere()).read()
    assert "not trading.robinhood.com" in row["error"]


ACCOUNTS = [
    {"id": "u1", "name": "Individual", "number": "Z12341234", "institution_name": "Fidelity", "status": "open",
     "is_paper": False, "balance": {"total": {"amount": 15363.23, "currency": "USD"}}},
    {"id": "u2", "name": "Roth IRA", "number": "Z99995678", "institution_name": "Fidelity", "status": "open",
     "is_paper": False, "balance": {"total": None}},
]


class Snap:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def request(self, method, url, json, timeout, headers):
        self.calls.append((method, url, json, headers))
        if url.startswith("https://api.snaptrade.com/accounts?"):
            return Resp(self.status, ACCOUNTS if self.status == 200 else {"detail": "Invalid signature"})
        return Resp(404, {"detail": url})


def test_snaptrade_signs_the_canonical_json_with_the_consumer_key():
    s = Snap()
    SnapTrade(ST, "client-1", "consumer-1", s, clock=lambda: 1700000000).read()
    [(method, url, body, h)] = s.calls
    assert (method, url, body) == ("GET", "https://api.snaptrade.com/accounts?clientId=client-1&timestamp=1700000000", None)
    signed = b'{"content":null,"path":"/accounts","query":"clientId=client-1&timestamp=1700000000"}'
    assert h == {"Signature": base64.b64encode(hmac.new(b"consumer-1", signed, hashlib.sha256).digest()).decode()}


def test_snaptrade_gives_one_row_per_connected_account():
    rows = SnapTrade(ST, "c", "k", Snap()).read()
    assert [(r["broker"], r["paper"], r["status"], r["equity"], r["error"]) for r in rows] == [
        ("fidelity_individual_1234", False, "open", 15363.23, None),
        ("fidelity_roth_ira_5678", False, "open", None, "snaptrade: no total balance for this account")]


def test_a_refused_snaptrade_read_is_one_row_without_the_key():
    [row] = SnapTrade(ST, "c", "consumer-1", Snap(status=401)).read()
    assert row["broker"] == "snaptrade" and "HTTP 401" in row["error"] and "consumer-1" not in row["error"]


def test_main_reads_every_source_whose_keys_are_set(conn, monkeypatch):
    class Both(Robinhood, Snap):
        def __init__(self):
            Robinhood.__init__(self)
            self.status = 200

        def request(self, method, url, json, timeout, headers):
            if url.startswith("https://api.snaptrade.com"):
                return Snap.request(self, method, url, json, timeout, headers)
            return Robinhood.request(self, method, url, json, timeout, headers)
    monkeypatch.setenv("ROBINHOOD_API_KEY", "rh-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", base64.b64encode(SEED).decode())
    monkeypatch.setenv("SNAPTRADE_CLIENT_ID", "c")
    monkeypatch.setenv("SNAPTRADE_CONSUMER_KEY", "k")
    assert broker.main(["read"], session=Both()) == 1  # the Roth IRA has no total
    assert conn.execute("SELECT broker, paper, equity FROM broker_balances ORDER BY broker").fetchall() == [
        ("fidelity_individual_1234", False, 15363.23), ("fidelity_roth_ira_5678", False, None),
        ("robinhood_crypto", False, 850.5)]
