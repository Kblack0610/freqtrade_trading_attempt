"""The one live door: any Orders adapter places only when lab/money/guard.py refusals() names no reason."""

import pytest

from lab.money import guard
from lab.crypto import league, suggest
from lab.market import Position
from lab.stages import live

CONFIG = {"live": {"stage": "s1", "venues": ["fake"]}, "book": {"stages": [{"name": "s1", "stake": 50.0, "max_open": 2}]}}


class FakeOrders:
    """An Orders adapter that records every place and holds what it is told to."""
    venue = "fake"
    resting = frozenset({"resting"})
    terminal = frozenset({"canceled"})

    def __init__(self, held=()):
        self.held, self.placed = [Position(t, 1, None) for t in held], []

    def place(self, ticker, price, count, client_id):
        self.placed.append((ticker, price, count, client_id))
        return {"order_id": client_id}

    def order(self, order_id):
        return {"status": "resting"}

    def cancel(self, order_id, ticker):
        return None

    def positions(self):
        return self.held

    def fills(self):
        return []


class Conn:
    """readiness and bankroll reads: ready for strategy s, the bankroll stopped when `stopped` is set."""
    def __init__(self, ready=True, stopped=None):
        self.ready, self.stopped, self.row = ready, stopped, None

    def execute(self, sql, args=()):
        self.row = (self.ready,) if "FROM readiness" in sql else ("2026-11-19", self.stopped) if self.stopped else None
        return self

    def fetchone(self):
        return self.row


def test_the_shipped_door_refuses_a_clean_order_before_the_client_places():
    client = FakeOrders()
    assert guard.GUARD_LIFTED is False
    with pytest.raises(live.Refused, match="no-real-money guard is on"):
        live.place(client, Conn(), "events", "s", "T", 0.40, 10, "c1", CONFIG)
    assert client.placed == []


@pytest.mark.parametrize("client, conn, price, count, want", [
    (FakeOrders(), Conn(), 0.40, 10, None),
    (FakeOrders(), Conn(), 0.60, 100, "stake_amount"),
    (FakeOrders(["A", "B"]), Conn(), 0.40, 10, "max_open_trades"),
    (FakeOrders(), Conn(ready=False), 0.40, 10, "not at ready"),
    (FakeOrders(), Conn(stopped="over the cap"), 0.40, 10, "bankroll is stopped"),
])
def test_with_the_guard_lifted_each_check_holds(monkeypatch, client, conn, price, count, want):
    monkeypatch.setattr(guard, "GUARD_LIFTED", True)
    if want is None:
        assert live.place(client, conn, "events", "s", "T", price, count, "c1", CONFIG) == {"order_id": "c1"}
        assert client.placed == [("T", price, count, "c1")]
        return
    with pytest.raises(live.Refused, match=want):
        live.place(client, conn, "events", "s", "T", price, count, "c1", CONFIG)
    assert client.placed == []


def test_a_venue_outside_the_live_list_is_refused(monkeypatch):
    monkeypatch.setattr(guard, "GUARD_LIFTED", True)
    client = FakeOrders()
    client.venue = "kalshi"
    with pytest.raises(live.Refused, match="'kalshi' is not one of"):
        live.place(client, Conn(), "events", "s", "T", 0.40, 10, "c1", CONFIG)


def test_the_league_names_the_guard_when_it_refuses(tmp_path, capsys):
    over = tmp_path / "overlay.json"
    over.write_text('{"dry_run": false}')
    with pytest.raises(SystemExit):
        league.guard([league.USER_DATA / "config.base.json", league.USER_DATA / "exchanges" / "kraken.json", over])
    err = capsys.readouterr().err
    assert "dry_run is not true" in err and "no-real-money guard is on" in err and "not at ready" not in err


def test_the_league_refuses_whatever_the_guard_says(tmp_path, monkeypatch, capsys):
    """The guard lifted and kraken a live venue at a stake the stage allows: the guard names nothing, the league refuses."""
    monkeypatch.setattr(guard, "GUARD_LIFTED", True)
    lifted = {"live": {"stage": "s1", "venues": ["kraken"]},
              "book": {"stages": [{"name": "s1", "stake": 1e9, "max_open": 1000}]}}
    monkeypatch.setattr(suggest, "load_config", lambda: lifted)
    over = tmp_path / "overlay.json"
    over.write_text('{"dry_run": false, "stake_amount": 10, "max_open_trades": 1}')
    with pytest.raises(SystemExit):
        league.guard([league.USER_DATA / "config.base.json", league.USER_DATA / "exchanges" / "kraken.json", over])
    assert capsys.readouterr().err.strip() == "league: refusing to start: dry_run is not true"


def test_the_league_refuses_when_the_guard_cannot_be_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(suggest, "load_config", lambda: {"live": {"stage": "gone"}, "book": {"stages": []}})
    over = tmp_path / "overlay.json"
    over.write_text('{"dry_run": false}')
    with pytest.raises(SystemExit) as e:
        league.guard([league.USER_DATA / "config.base.json", league.USER_DATA / "exchanges" / "kraken.json", over])
    err = capsys.readouterr().err
    assert e.value.code == 2 and "dry_run is not true" in err and "the guard could not read" in err


def test_a_locked_door_reads_neither_the_client_nor_the_database():
    client = FakeOrders()
    client.positions = lambda: pytest.fail("read the client")
    conn = Conn()
    conn.execute = lambda *a: pytest.fail("read the database")
    with pytest.raises(live.Refused, match="guard is on"):
        live.place(client, conn, "events", "s", "T", 0.40, 10, "c1", CONFIG)


def test_a_top_up_of_a_held_ticker_takes_no_new_slot(monkeypatch):
    monkeypatch.setattr(guard, "GUARD_LIFTED", True)
    client = FakeOrders(["A", "B"])
    assert live.place(client, Conn(), "events", "s", "A", 0.40, 10, "c1", CONFIG) == {"order_id": "c1"}
