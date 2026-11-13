import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from lab.markets import kalshi_trade
from lab.markets.kalshi_trade import KalshiDemo, NotDemo

DEMO = {"base_url": "https://external-api.demo.kalshi.co/trade-api/v2", "timeout_s": 5, "page_gap_s": 0, "retries": 2, "read_polls": 3, "read_poll_s": 0.25}


def pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.body, self.text, self.headers = status, body, str(body), headers or {}

    def json(self):
        return self.body


class Demo:
    """A demo exchange: one resting order, cancellable; records every request."""

    def __init__(self, cancel_works=True, unseen=0, stale=0, throttled=0):
        self.calls, self.status, self.cancel_works, self.throttled = [], None, cancel_works, throttled
        self.unseen, self.stale = unseen, stale  # reads that lag the place (404) and the cancel (still resting)

    def request(self, method, url, params, json, timeout, headers):
        path = url.split("/trade-api/v2", 1)[1].split("?")[0]
        self.calls.append((method, path, params, json, headers))
        if method == "GET" and path == "/markets":
            return Resp(200, {"markets": [{"ticker": "T-THIN", "yes_ask_dollars": "0.0100", "exchange_index": 0},
                                          {"ticker": "T-SHARD3", "yes_ask_dollars": "0.4200", "exchange_index": 3},
                                          {"ticker": "T-OK", "yes_ask_dollars": "0.4200", "exchange_index": 0}]})
        if method == "POST" and path == "/portfolio/events/orders":
            if self.throttled:
                self.throttled -= 1
                return Resp(429, {"error": "too_many_requests"}, {"Retry-After": "1"})
            self.status = "resting"
            return Resp(201, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "1.00", "ts_ms": 1})
        if method == "GET" and path == "/portfolio/orders/o1":
            if self.unseen:
                self.unseen -= 1
                return Resp(404, {"error": {"code": "not_found"}})
            if self.status == "canceled" and self.stale:
                self.stale -= 1
                return Resp(200, {"order": {"order_id": "o1", "status": "resting"}})
            return Resp(200, {"order": {"order_id": "o1", "status": self.status}})
        if method == "DELETE" and path == "/portfolio/events/orders/o1":
            if not self.cancel_works:
                return Resp(503, {"error": "busy"})
            self.status = "canceled"
            return Resp(200, {"order_id": "o1", "reduced_by": "1.00", "ts_ms": 2})
        return Resp(404, {"error": path})


@pytest.mark.parametrize("url", ["https://api.elections.kalshi.com/trade-api/v2",
                                 "https://external-api.kalshi.com/trade-api/v2",
                                 "http://external-api.demo.kalshi.co/trade-api/v2",
                                 "https://external-api.demo.kalshi.co.evil.example/trade-api/v2"])
def test_anything_but_the_demo_exchange_is_refused(url):
    with pytest.raises(NotDemo):
        KalshiDemo({"base_url": url}, "k", pem(RSA))


def test_an_rsa_signature_covers_timestamp_method_and_full_path_without_the_query():
    s = Demo()
    c = KalshiDemo(DEMO, "key-1", pem(RSA), session=s, clock=lambda: 1715793600.123)
    c.call("GET", "/portfolio/orders/o1?x=1")
    h = s.calls[0][4]
    assert h["KALSHI-ACCESS-KEY"] == "key-1" and h["KALSHI-ACCESS-TIMESTAMP"] == "1715793600123"
    RSA.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            b"1715793600123GET/trade-api/v2/portfolio/orders/o1",
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())


def test_an_ed25519_key_signs_too():
    key = ed25519.Ed25519PrivateKey.generate()
    s = Demo()
    KalshiDemo(DEMO, "k", pem(key), session=s, clock=lambda: 1.0).call("DELETE", "/portfolio/events/orders/o1")
    key.public_key().verify(base64.b64decode(s.calls[0][4]["KALSHI-ACCESS-SIGNATURE"]),
                            b"1000DELETE/trade-api/v2/portfolio/events/orders/o1")


def test_the_drill_rests_one_contract_then_cancels_it(conn):
    """T-SHARD3 could rest too, but sits on an exchange index the funds are not on (insufficient_shard_balance)."""
    s = Demo()
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s), 0.01, 0)
    assert (row["ticker"], row["order_id"], row["status"], row["error"]) == ("T-OK", "o1", "canceled", None)
    assert row["canceled_at"] is not None
    body = s.calls[1][3]
    assert {k: body[k] for k in ("ticker", "side", "count", "price", "post_only")} == \
        {"ticker": "T-OK", "side": "bid", "count": "1.00", "price": "0.0100", "post_only": True}
    assert [c[:2] for c in s.calls[1:]] == [("POST", "/portfolio/events/orders"), ("GET", "/portfolio/orders/o1"),
                                           ("DELETE", "/portfolio/events/orders/o1"), ("GET", "/portfolio/orders/o1"),
                                           ("GET", "/portfolio/orders/o1")]
    assert s.calls[3][2] == {"market_ticker": "T-OK"}


def test_the_drill_pages_past_empty_books_to_find_a_market(conn):
    """Most demo markets ask 1.00 (no sellers), so the first page can hold none the drill can rest under."""
    pages = {None: {"markets": [{"ticker": "T-EMPTY", "yes_ask_dollars": "1.0000", "exchange_index": 0}], "cursor": "c2"},
             "c2": {"markets": [{"ticker": "T-OK", "yes_ask_dollars": "0.4200", "exchange_index": 0}], "cursor": "c3"}}

    class Paged(Demo):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if method == "GET" and url.endswith("/markets"):
                self.calls.append((method, "/markets", params, json, headers))
                return Resp(200, pages[(params or {}).get("cursor")])
            return super().request(method, url, params=params, json=json, timeout=timeout, headers=headers)

    s = Paged()
    row = kalshi_trade.drill(conn, KalshiDemo({**DEMO, "market_pages": 3}, "k", pem(RSA), session=s), 0.01, 0)
    assert (row["ticker"], row["status"]) == ("T-OK", "canceled")
    assert [c[2].get("cursor") for c in s.calls if c[1] == "/markets"] == [None, "c2"], "stops at the first hit"


def test_no_restable_market_in_market_pages_fails_the_drill(conn):
    class Empty(Demo):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            self.calls.append((method, "/markets", params, json, headers))
            return Resp(200, {"markets": [{"ticker": "T-EMPTY", "yes_ask_dollars": "1.0000", "exchange_index": 0}], "cursor": "more"})

    s = Empty()
    with pytest.raises(RuntimeError, match="in 2 pages"):
        kalshi_trade.drill(conn, KalshiDemo({**DEMO, "market_pages": 2}, "k", pem(RSA), session=s), 0.01, 0)
    assert len(s.calls) == 2


def test_a_failed_cancel_is_recorded_and_fails_the_drill(conn):
    s = Demo(cancel_works=False)
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s, sleep=[].append), 0.01, 0, ticker="T-OK")
    assert row["status"] == "resting" and row["canceled_at"] is None and "HTTP 503" in row["error"]
    assert [c[0] for c in s.calls].count("POST") == 1  # never retried
    assert [c[0] for c in s.calls].count("DELETE") == 1 + DEMO["retries"], "a cancel by id is retried on a 5xx"


def test_a_throttled_place_is_sent_again_with_a_fresh_signature():
    """A 429 means Kalshi did not act on the order, so it is retried, after Retry-After, signed again."""
    ticks, slept, s = iter([1.0, 2.0]), [], Demo(throttled=1)
    c = KalshiDemo(DEMO, "k", pem(RSA), session=s, clock=lambda: next(ticks), sleep=slept.append)
    assert c.place("T-OK", 0.01, 1, "cid")["order_id"] == "o1"
    first, second = (h for m, _, _, _, h in s.calls if m == "POST")
    assert (first["KALSHI-ACCESS-TIMESTAMP"], second["KALSHI-ACCESS-TIMESTAMP"]) == ("1000", "2000")
    for h in (first, second):
        RSA.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                                f"{h['KALSHI-ACCESS-TIMESTAMP']}POST/trade-api/v2/portfolio/events/orders".encode(),
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())
    assert slept[0] == 1.0, "Retry-After"


def test_reads_that_lag_the_place_and_the_cancel_still_pass_the_drill(conn):
    s, slept = Demo(unseen=2, stale=2), []
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s, sleep=slept.append), 0.01, 0, ticker="T-OK")
    assert (row["status"], row["error"]) == ("canceled", None)
    assert slept == [0.25] * 4
    assert [c[0] for c in s.calls].count("POST") == 1


def test_an_order_never_seen_is_canceled_and_fails_the_drill(conn):
    s, slept = Demo(unseen=99), []
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s, sleep=slept.append), 0.01, 0, ticker="T-OK")
    assert row["order_id"] == "o1" and row["canceled_at"] is None and "HTTP 404" in row["error"]
    assert s.status == "canceled"  # not left on the book
    assert [c[0] for c in s.calls].count("POST") == 1


def test_a_cancel_stuck_resting_fails_the_drill_within_its_bound(conn):
    s, slept = Demo(stale=99), []
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s, sleep=slept.append), 0.01, 0, ticker="T-OK")
    assert row["status"] == "resting" and "after the cancel" in row["error"]
    assert slept == [0.25] * 3


def test_the_demo_config_points_at_the_demo():
    cfg = kalshi_trade.load_config()["demo"]["kalshi"]
    KalshiDemo(cfg, "k", pem(RSA))  # does not raise
