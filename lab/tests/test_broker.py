"""The Alpaca paper client: the host guard, the account read, and the drill, against a fake paper API."""

import base64
import datetime as dt
import hashlib
import hmac
from urllib.parse import urlencode

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab import broker
from lab.broker import AlpacaPaper, KrakenRead, NotPaper, PolymarketUSRead, RobinhoodCrypto, SnapTrade, WrongHost

PAPER = {"base_url": "https://paper-api.alpaca.markets", "timeout_s": 5, "page_gap_s": 0, "retries": 2}
ACCOUNT = {"id": "a1", "status": "ACTIVE", "currency": "USD", "equity": "100012.50", "cash": "99000.00",
           "buying_power": "199000.00"}


def NO_SLEEP(_):
    pass


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        if self.status_code == 204:
            raise ValueError("no body")
        return self.body


class Paper:
    """A paper account with one order, cancelled after `pending` reads of pending_cancel; records every request.
    `stale` reads right after the DELETE still return the status from before it, as the paper API did on 2026-11-12."""

    def __init__(self, placed="accepted", pending=0, cancel_status=204, account=200, stale=0):
        self.calls, self.status, self.placed = [], None, placed
        self.pending, self.cancel_status, self.account_status, self.stale = pending, cancel_status, account, stale

    def request(self, method, url, json, timeout, headers, params=None):
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
            if self.status == "pending_cancel" and self.stale > 0:
                self.stale -= 1
                return Resp(200, {"id": "o1", "status": self.placed})
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


def test_the_drill_waits_out_a_read_that_still_says_new_after_the_cancel(conn):
    """The 2026-11-12 14:40Z failure: the read right after the DELETE still said new, and the drill took it as final."""
    s, slept = Paper(placed="new", stale=1, pending=1), []
    row = broker.rest_and_cancel(conn, client(s, slept), "SPY", 1.0)
    assert (row["status"], row["error"]) == ("canceled", None)
    assert len(slept) == 2


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
    assert [c[0] for c in s.calls].count("DELETE") == 1 + PAPER["retries"], "a cancel by id is retried on a 5xx"


def test_a_cancel_stuck_pending_fails_the_drill_within_its_bound(conn):
    s, slept = Paper(pending=99), []
    row = broker.rest_and_cancel(conn, client(s, slept), "SPY", 1.0)
    assert row["status"] == "pending_cancel" and "after the cancel" in row["error"]
    assert len(slept) == broker.CANCEL_POLLS


def test_main_reads_and_stores_with_keys_set(conn, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key-1")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret-1")
    assert broker.main(["read"], session=Paper(), sleep=NO_SLEEP) == 0
    assert conn.execute("SELECT equity, error FROM broker_balances").fetchall() == [(100012.5, None)]


def test_main_exits_1_when_the_read_failed(conn, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "key-1")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "secret-1")
    assert broker.main(["read"], session=Paper(account=403), sleep=NO_SLEEP) == 1
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
RH = {"base_url": "https://trading.robinhood.com", "timeout_s": 5, "page_gap_s": 0, "quote": "USD"}
ST = {"base_url": "https://api.snaptrade.com", "timeout_s": 5, "page_gap_s": 0}


class Robinhood:
    """A crypto account holding BTC over two holdings pages, and ETH at zero; records every request."""

    def __init__(self, account=200, prices=("BTC-USD",)):
        self.calls, self.account_status, self.priced = [], account, prices

    def request(self, method, url, json, timeout, headers, params=None):
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
                                     (SnapTrade, {"base_url": "https://api.snaptrade.example"}),
                                     (KrakenRead, {"base_url": "https://api.kraken.com.evil.example"}),
                                     (PolymarketUSRead, {"base_url": "https://api.polymarket.us.evil.example"})])
def test_a_read_only_client_refuses_any_other_host(cls, cfg):
    with pytest.raises(WrongHost):
        cls(cfg, "k", base64.b64encode(SEED).decode())


def test_the_shipped_config_points_at_each_platform():
    cfg = broker.tomllib.loads(broker.CONFIG.read_text())
    RobinhoodCrypto(cfg["robinhood"], "k", base64.b64encode(SEED).decode())
    SnapTrade(cfg["snaptrade"], "c", "k")
    KrakenRead(cfg["kraken"], "k", KR_SECRET)
    PolymarketUSRead(cfg["polymarket_us"], "k", PM_SECRET)


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
        def request(self, method, url, json, timeout, headers, params=None):
            r = super().request(method, url, json, timeout, headers, params)
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

    def request(self, method, url, json, timeout, headers, params=None):
        url = f"{url}?{urlencode(params)}" if params else url
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

        def request(self, method, url, json, timeout, headers, params=None):
            if url.startswith("https://api.snaptrade.com"):
                return Snap.request(self, method, url, json, timeout, headers, params)
            return Robinhood.request(self, method, url, json, timeout, headers, params)
    monkeypatch.setenv("ROBINHOOD_API_KEY", "rh-key")
    monkeypatch.setenv("ROBINHOOD_PRIVATE_KEY", base64.b64encode(SEED).decode())
    monkeypatch.setenv("SNAPTRADE_CLIENT_ID", "c")
    monkeypatch.setenv("SNAPTRADE_CONSUMER_KEY", "k")
    assert broker.main(["read"], session=Both(), sleep=NO_SLEEP) == 1  # the Roth IRA has no total
    assert conn.execute("SELECT broker, paper, equity FROM broker_balances ORDER BY broker").fetchall() == [
        ("fidelity_individual_1234", False, 15363.23), ("fidelity_roth_ira_5678", False, None),
        ("robinhood_crypto", False, 850.5)]


# Kraken: a real account, read only, over POST.

KR = {"base_url": "https://api.kraken.com", "timeout_s": 5, "page_gap_s": 0, "retries": 2}
KR_SECRET = base64.b64encode(b"kraken-secret-bytes").decode()


class Kraken:
    """A spot account with USD cash, some BTC and a zero ETH line; records every request. `limited` 429s come first."""

    def __init__(self, error=None, limited=0):
        self.calls, self.error, self.limited = [], error, limited

    def request(self, method, url, json, timeout, headers, params=None):
        path = url.removeprefix("https://api.kraken.com")
        self.calls.append((method, path, json, headers))
        if self.limited:
            self.limited -= 1
            return Resp(429, {"error": ["EAPI:Rate limit exceeded"]})
        if self.error:
            return Resp(200, {"error": [self.error], "result": {}})
        if path == "/0/private/Balance":
            return Resp(200, {"error": [], "result": {"ZUSD": "120.5000", "XXBT": "0.0100000000", "XETH": "0.0000000000"}})
        if path == "/0/private/TradeBalance":
            return Resp(200, {"error": [], "result": {"eb": "720.5000", "tb": "720.5000"}})
        return Resp(404, {"error": [f"EGeneral:Unknown method {path}"]})


def kr(s, sleep=None):
    return KrakenRead(KR, "kr-key", KR_SECRET, s, clock=lambda: 1700000000, sleep=sleep or NO_SLEEP)


def test_kraken_reads_cash_holdings_and_its_own_usd_valuation():
    s = Kraken()
    [row] = kr(s).read()
    assert row == {"broker": "kraken", "paper": False, "status": "open", "currency": "USD", "equity": 720.5,
                   "cash": 120.5, "buying_power": None, "positions": 1, "error": None}
    assert [(m, p) for m, p, _, _ in s.calls] == [("POST", "/0/private/Balance"), ("POST", "/0/private/TradeBalance")]
    assert s.calls[1][2]["asset"] == "ZUSD"


def test_kraken_signs_path_and_the_hash_of_nonce_and_body_with_the_secret():
    s = Kraken()
    kr(s).read()
    for _, path, body, h in s.calls:
        digest = hashlib.sha256((body["nonce"] + broker.json.dumps(body)).encode()).digest()
        want = hmac.new(base64.b64decode(KR_SECRET), path.encode() + digest, hashlib.sha512).digest()
        assert h == {"API-Key": "kr-key", "API-Sign": base64.b64encode(want).decode()}
    nonces = [int(c[2]["nonce"]) for c in s.calls]
    assert nonces == sorted(set(nonces)) and nonces[0] == 1700000000 * 10**9


def test_a_kraken_retry_sends_a_fresh_nonce():
    s = Kraken(limited=1)
    [row] = kr(s).read()
    assert row["error"] is None
    first, retry = s.calls[0][2]["nonce"], s.calls[1][2]["nonce"]
    assert int(retry) > int(first), "Kraken refuses a nonce it has seen"


@pytest.mark.parametrize("path", ["/0/private/AddOrder", "/0/private/Withdraw", "/0/private/CancelAll"])
def test_kraken_refuses_any_path_but_its_two_reads_before_sending(path):
    s = Kraken()
    with pytest.raises(ValueError, match="refusing"):
        kr(s).post(path, {"pair": "XBTUSD"})
    assert s.calls == []


def test_a_kraken_error_is_a_row_without_the_key():
    [row] = kr(Kraken(error="EGeneral:Permission denied")).read()
    assert row["equity"] is None and row["error"] == "kraken POST /0/private/Balance: EGeneral:Permission denied"
    assert "kr-key" not in row["error"] and KR_SECRET not in row["error"]


def test_main_reads_kraken_when_its_keys_are_set(conn, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "kr-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", KR_SECRET)
    assert broker.main(["read"], session=Kraken(), sleep=NO_SLEEP) == 0
    assert conn.execute("SELECT broker, paper, equity, cash, positions FROM broker_balances").fetchall() == [
        ("kraken", False, 720.5, 120.5, 1)]


# Polymarket US: a real account, read only, GET with an Ed25519 signature.

PM = {"base_url": "https://api.polymarket.us", "timeout_s": 5, "page_gap_s": 0, "retries": 2, "currency": "USD"}
PM_SECRET = base64.b64encode(SEED + bytes(32)).decode()   # the developer page hands out seed + public key, base64


class Polymarket:
    """An account with USD cash and one open market plus one closed out; records every request.
    `balances` replaces the balance list (an unfunded account has none); `status` answers every call."""

    def __init__(self, balances=None, status=200, eof=True):
        self.calls, self.status, self.eof = [], status, eof
        self.balances = [{"currency": "USD", "currentBalance": 40.25, "buyingPower": 38.0, "assetNotional": 12.5}] \
            if balances is None else balances

    def request(self, method, url, json, timeout, headers, params=None):
        path = url.removeprefix("https://api.polymarket.us")
        self.calls.append((method, path, headers))
        if self.status != 200:
            return Resp(self.status, {"message": "API key not found"})
        if path == "/v1/account/balances":
            return Resp(200, {"balances": self.balances})
        if path == "/v1/portfolio/positions":
            return Resp(200, {"positions": {"nba-lal-bos": {"netPosition": "5"}, "nfl-kc-buf": {"netPosition": "0"}},
                              "eof": self.eof})
        return Resp(404, {"message": path})


def pm(s):
    return PolymarketUSRead(PM, "pm-key", PM_SECRET, s, clock=lambda: 1700000000.5, sleep=NO_SLEEP)


def test_polymarket_us_reads_cash_buying_power_and_the_value_of_open_markets():
    s = Polymarket()
    [row] = pm(s).read()
    assert row == {"broker": "polymarket_us", "paper": False, "status": "open", "currency": "USD", "equity": 52.75,
                   "cash": 40.25, "buying_power": 38.0, "positions": 1, "error": None}
    assert [(m, p) for m, p, _ in s.calls] == [("GET", "/v1/account/balances"), ("GET", "/v1/portfolio/positions")]


def test_polymarket_us_signs_millisecond_timestamp_method_and_path_with_the_ed25519_key():
    s = Polymarket()
    pm(s).read()
    for _, path, h in s.calls:
        assert (h["X-PM-Access-Key"], h["X-PM-Timestamp"]) == ("pm-key", "1700000000500")
        Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(
            base64.b64decode(h["X-PM-Signature"]), f"1700000000500GET{path}".encode())


def test_an_unfunded_polymarket_us_account_reads_as_zero():
    [row] = pm(Polymarket(balances=[])).read()
    assert (row["cash"], row["equity"], row["buying_power"], row["error"]) == (0.0, 0.0, 0.0, None)


def test_a_polymarket_us_balance_in_another_currency_is_an_error_not_a_zero():
    [row] = pm(Polymarket(balances=[{"currency": "USDC", "currentBalance": 9}])).read()
    assert row["cash"] is None and "no USD balance among ['USDC']" in row["error"]


def test_a_polymarket_us_positions_list_past_one_page_says_its_count_is_short():
    [row] = pm(Polymarket(eof=False)).read()
    assert row["positions"] == 1 and "more than one page" in row["error"]


def test_a_refused_polymarket_us_read_is_a_row_without_the_key():
    [row] = pm(Polymarket(status=401)).read()
    assert row["equity"] is None and row["error"].startswith("polymarket_us GET /v1/account/balances: HTTP 401")
    assert "pm-key" not in row["error"] and PM_SECRET not in row["error"]


def test_main_reads_polymarket_us_when_its_keys_are_set(conn, monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "pm-key")
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", PM_SECRET)
    assert broker.main(["read"], session=Polymarket(), sleep=NO_SLEEP) == 0
    assert conn.execute("SELECT broker, paper, equity, cash, positions FROM broker_balances").fetchall() == [
        ("polymarket_us", False, 52.75, 40.25, 1)]
