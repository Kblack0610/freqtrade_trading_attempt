import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from lab.markets import kalshi_trade
from lab.markets.kalshi_trade import KalshiDemo, NotDemo

DEMO = {"base_url": "https://external-api.demo.kalshi.co/trade-api/v2", "timeout_s": 5}


def pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Resp:
    def __init__(self, status, body):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        return self.body


class Demo:
    """A demo exchange: one resting order, cancellable; records every request."""

    def __init__(self, cancel_works=True):
        self.calls, self.status, self.cancel_works = [], None, cancel_works

    def request(self, method, url, params, json, timeout, headers):
        path = url.split("/trade-api/v2", 1)[1].split("?")[0]
        self.calls.append((method, path, params, json, headers))
        if method == "GET" and path == "/markets":
            return Resp(200, {"markets": [{"ticker": "T-THIN", "yes_ask_dollars": "0.0100"},
                                          {"ticker": "T-OK", "yes_ask_dollars": "0.4200"}]})
        if method == "POST" and path == "/portfolio/events/orders":
            self.status = "resting"
            return Resp(201, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "1.00", "ts_ms": 1})
        if method == "GET" and path == "/portfolio/orders/o1":
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
    s = Demo()
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s), 0.01)
    assert (row["ticker"], row["order_id"], row["status"], row["error"]) == ("T-OK", "o1", "canceled", None)
    assert row["canceled_at"] is not None
    body = s.calls[1][3]
    assert {k: body[k] for k in ("ticker", "side", "count", "price", "post_only")} == \
        {"ticker": "T-OK", "side": "bid", "count": "1.00", "price": "0.0100", "post_only": True}
    assert [c[:2] for c in s.calls[1:]] == [("POST", "/portfolio/events/orders"), ("GET", "/portfolio/orders/o1"),
                                           ("DELETE", "/portfolio/events/orders/o1"), ("GET", "/portfolio/orders/o1")]
    assert s.calls[3][2] == {"market_ticker": "T-OK"}


def test_a_failed_cancel_is_recorded_and_fails_the_drill(conn):
    s = Demo(cancel_works=False)
    row = kalshi_trade.drill(conn, KalshiDemo(DEMO, "k", pem(RSA), session=s), 0.01, ticker="T-OK")
    assert row["status"] == "resting" and row["canceled_at"] is None and "HTTP 503" in row["error"]
    assert [c[0] for c in s.calls].count("POST") == 1  # never retried


def test_the_demo_config_points_at_the_demo():
    cfg = kalshi_trade.load_config()["demo"]["kalshi"]
    KalshiDemo(cfg, "k", pem(RSA))  # does not raise
