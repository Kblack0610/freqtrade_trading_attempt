import pytest

from lab.markets import manifold_trade
from lab.markets.manifold_trade import ManifoldBets, NotManifold

CFG = {"base_url": "https://api.manifold.markets", "timeout_s": 5, "page_gap_s": 0, "drill_mana": 1, "expires_s": 600}


class Resp:
    def __init__(self, status, body):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        return self.body


class Manifold:
    """Manifold's bet routes: one limit bet that rests, fills or cancels as told; records every request."""

    def __init__(self, cancel_works=True, fill=0.0, stale=0):
        """`stale` reads of the bets list after the cancel still show the bet open, as the live list did."""
        self.calls, self.bet, self.cancel_works, self.fill, self.stale = [], None, cancel_works, fill, stale

    def request(self, method, url, params, json, timeout, headers):
        path = url.removeprefix("https://api.manifold.markets")
        self.calls.append((method, path, params, json, headers))
        if method == "GET" and path == "/v0/me":
            return Resp(200, {"id": "u1", "username": "bot", "balance": 500.0})
        if method == "GET" and path == "/v0/search-markets":
            return Resp(200, [{"id": "LOW", "mechanism": "cpmm-1", "token": "MANA", "isResolved": False, "probability": 0.04},
                              {"id": "CASH", "mechanism": "cpmm-1", "token": "CASH", "isResolved": False, "probability": 0.5},
                              {"id": "OK", "mechanism": "cpmm-1", "token": "MANA", "isResolved": False, "probability": 0.42}])
        if method == "POST" and path == "/v0/bet":
            self.bet = {"id": "b1", "contractId": json["contractId"], "isFilled": False, "isCancelled": False,
                        "amount": self.fill, "orderAmount": json["amount"], "limitProb": json["limitProb"]}
            return Resp(200, {**self.bet, "betId": "b1"})
        if method == "GET" and path == "/v0/bets":
            if self.bet and self.bet["isCancelled"] and self.stale > 0:
                self.stale -= 1
                return Resp(200, [{"id": "other"}, {**self.bet, "isCancelled": False}])
            return Resp(200, [{"id": "other"}, self.bet] if self.bet else [])
        if method == "POST" and path == "/v0/bet/cancel/b1":
            if not self.cancel_works:
                return Resp(503, {"message": "busy"})
            self.bet["isCancelled"] = True
            return Resp(200, self.bet)
        return Resp(404, {"message": path})


@pytest.mark.parametrize("url", ["http://api.manifold.markets", "https://manifold.markets",
                                 "https://api.manifold.markets.evil.example", "https://evil.example"])
def test_anything_but_manifolds_api_is_refused(url):
    with pytest.raises(NotManifold):
        ManifoldBets({"base_url": url}, "k")


@pytest.mark.parametrize("bet,word", [({"isCancelled": True, "amount": 0}, "canceled"),
                                      ({"isFilled": True, "amount": 1}, "filled"),
                                      ({"amount": 0.4}, "partial"), ({"amount": 0}, "open")])
def test_a_bet_reads_as_the_drills_word(bet, word):
    assert manifold_trade.status(bet) == word


def test_the_key_rides_every_request_and_balance_reads_me():
    s = Manifold()
    assert ManifoldBets(CFG, "sekrit", session=s).balance() == {"username": "bot", "balance": 500.0}
    assert s.calls[0][4] == {"Authorization": "Key sekrit"}


def test_the_drill_rests_one_limit_bet_then_cancels_it(conn):
    s = Manifold()
    row = manifold_trade.drill(conn, ManifoldBets(CFG, "k", session=s), 0.01, 0.10)
    assert (row["ticker"], row["order_id"], row["status"], row["error"]) == ("OK", "b1", "canceled", None)
    assert row["canceled_at"] is not None
    bet = next(c for c in s.calls if c[:2] == ("POST", "/v0/bet"))[3]
    assert bet == {"contractId": "OK", "outcome": "YES", "amount": 1.0, "limitProb": 0.01, "expiresMillisAfter": 600000}
    assert [c[:2] for c in s.calls if c[1] != "/v0/me"][1:] == [
        ("POST", "/v0/bet"), ("GET", "/v0/bets"), ("POST", "/v0/bet/cancel/b1"), ("GET", "/v0/bets"), ("GET", "/v0/bets")]
    reads = [c[2] for c in s.calls if c[1] == "/v0/bets"]
    assert all(p["userId"] == "u1" and p["contractId"] == "OK" for p in reads)


def test_the_drill_waits_out_a_bets_list_that_still_says_open_after_the_cancel(conn):
    s, slept = Manifold(stale=2), []
    row = manifold_trade.drill(conn, ManifoldBets(CFG, "k", session=s, sleep=slept.append), 0.01, 0.10, ticker="OK")
    assert (row["status"], row["error"]) == ("canceled", None)
    assert len(slept) == 2


def test_a_bet_that_filled_at_all_fails_the_drill(conn):
    s = Manifold(fill=0.5)
    row = manifold_trade.drill(conn, ManifoldBets(CFG, "k", session=s), 0.01, 0.10, ticker="OK")
    assert row["status"] == "partial" and "not resting" in row["error"]


def test_a_failed_cancel_is_recorded_and_fails_the_drill(conn):
    s = Manifold(cancel_works=False)
    row = manifold_trade.drill(conn, ManifoldBets(CFG, "k", session=s), 0.01, 0.10, ticker="OK")
    assert row["status"] == "open" and row["canceled_at"] is None and "HTTP 503" in row["error"]
    assert [c[:2] for c in s.calls].count(("POST", "/v0/bet")) == 1  # never retried


def test_no_market_priced_high_enough_is_an_error():
    with pytest.raises(RuntimeError, match="no open binary mana market"):
        manifold_trade.drill(None, ManifoldBets(CFG, "k", session=Manifold()), 0.01, 0.90)


def test_the_config_points_at_manifold():
    cfg = manifold_trade.load_config()["demo"]["manifold"]
    ManifoldBets(cfg, "k")  # does not raise
    assert cfg["drill_price"] < cfg["min_prob"]


class Account(Manifold):
    """Three settled bets: a yes buy filled in two matches, a no buy, and a sale of part of the yes."""
    BETS = [{"id": "b3", "contractId": "A", "outcome": "YES", "shares": -4.0, "amount": -2.4,
             "fills": [{"shares": -4.0, "amount": -2.4, "timestamp": 1763046000000}]},
            {"id": "b2", "contractId": "B", "outcome": "NO", "shares": 5.0, "amount": 2.0,
             "fills": [{"shares": 5.0, "amount": 2.0, "timestamp": 1763042400000}]},
            {"id": "b1", "contractId": "A", "outcome": "YES", "shares": 10.0, "amount": 5.0,
             "fills": [{"shares": 6.0, "amount": 3.0, "timestamp": 1763038800000},
                       {"shares": 4.0, "amount": 2.0, "timestamp": 1763038860000}]}]

    def request(self, method, url, params, json, timeout, headers):
        if method == "GET" and url.endswith("/v0/bets"):
            assert params == {"userId": "u1", "limit": 1000}
            return Resp(200, self.BETS)
        return super().request(method, url, params, json, timeout, headers)


def test_positions_sum_the_bets_and_fills_list_each_match_newest_first():
    c = ManifoldBets(CFG, "k", session=Account(), sleep=lambda s: None)
    assert [(p.ticker, p.count, p.cost) for p in c.positions()] == [("A", 6.0, pytest.approx(2.6)), ("B", -5.0, 2.0)]
    fills = c.fills()
    assert [(f.order_id, f.count) for f in fills] == [("b3", -4.0), ("b2", -5.0), ("b1", 4.0), ("b1", 6.0)]
    assert fills[1].price == pytest.approx(0.6) and fills[0].price == pytest.approx(0.6)
