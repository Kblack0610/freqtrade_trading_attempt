import pytest

from lab import guard, suggest
from lab.broker import AlpacaPaper
from lab.markets.kalshi_trade import KalshiDemo
from lab.markets.manifold_trade import ManifoldBets
from lab.markets.venue import Orders

LIVE = {"venues": ["kraken"]}
STAGE = {"stake": 50.0, "max_open": 3}


def test_the_shipped_guard_refuses_whatever_else_holds():
    assert guard.GUARD_LIFTED is False
    assert guard.refusals("kraken", 50.0, 3, LIVE, STAGE, True, None) == ["the no-real-money guard is on (docs/live.md)"]


@pytest.mark.parametrize("venue, stake, slots, ready, stopped, want", [
    ("kraken", 50.0, 3, True, None, None),
    ("kraken", 51.0, 3, True, None, "stake_amount"),
    ("kraken", float("inf"), 3, True, None, "stake_amount"),
    ("kraken", 50.0, -1, True, None, "max_open_trades"),
    ("coinbase", 50.0, 3, True, None, "'coinbase' is not one of"),
    ("kraken", 50.0, 3, False, None, "not at ready"),
    ("kraken", 50.0, 3, True, "over the cap", "the bankroll is stopped: over the cap"),
])
def test_each_generic_check(monkeypatch, venue, stake, slots, ready, stopped, want):
    monkeypatch.setattr(guard, "GUARD_LIFTED", True)
    problems = guard.refusals(venue, stake, slots, LIVE, STAGE, ready, stopped)
    assert problems == [] if want is None else len(problems) == 1 and want in problems[0]


def test_the_live_stage_names_its_row_of_book_stages():
    cfg = suggest.load_config()
    assert guard.stage_of(cfg["live"], cfg["book"])["name"] == cfg["live"]["stage"]


class Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, sql):
        assert "FROM bankroll" in sql
        return self

    def fetchone(self):
        return self.row


@pytest.mark.parametrize("row, want", [(None, None), ((None, None), None), (("2026-11-13", "over the cap"), "over the cap")])
def test_killed_reads_the_bankroll_row(row, want):
    assert guard.killed(Conn(row)) == want


@pytest.mark.parametrize("cls", [AlpacaPaper, KalshiDemo, ManifoldBets])
def test_every_order_client_is_an_orders(cls):
    members = {m for m in Orders.__dict__ if not m.startswith("_")} | set(Orders.__annotations__)
    assert members == {"venue", "resting", "terminal", "place", "order", "cancel", "positions", "fills"}
    assert all(hasattr(cls, m) for m in members)
