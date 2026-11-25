"""The futures league (lab/futures/paper.py) on a fake ticker client: longs and shorts open, fill, mark and settle, and
funding changes the profit by the recorded rate."""

import copy
import datetime as dt

import pytest

from lab.futures import paper as futures
from lab.futures.paper import Quote

NOW = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)
LATER = NOW + dt.timedelta(hours=24)
SHORT, LONG = "BTC/USDT:USDT", "ETH/USDT:USDT"
PARAMS = {"threshold": 0.0001, "hold_hours": 24}
ACCOUNT = futures.account_of("funding", futures.variant_of(PARAMS))


def cfg(**over):
    c = copy.deepcopy(futures.load_config())
    c["families"] = {"funding": PARAMS}
    c["venues"] = ["okx"]
    c["paper"] |= {"stake": 1000.0, "capital": 10000.0, "taker_bps": 5, "max_open": 4, "max_spread_bps": 30, **over}
    return c


class Tickers:
    def __init__(self, books, at):
        self.books, self.at = books, at

    def quotes(self, symbols):
        return {("okx", s): Quote(b, a, self.at) for s, (b, a) in self.books.items() if s in symbols.get("okx", [])}


def rates(conn, pair, *rows):
    """Funding rows of one swap: (hours from NOW, rate) each."""
    for h, r in rows:
        conn.execute("INSERT INTO funding_rates (exchange, pair, ts, rate) VALUES ('okx', %s, %s, %s)",
                     (pair, NOW + dt.timedelta(hours=h), r))


def run(conn, books, now, quoted=None, **over):
    return futures.run_pass(conn, cfg(**over), Tickers(books, quoted or now), now)


def positions(conn):
    return {m: r for m, *r in conn.execute(
        "SELECT market_id, side, contracts, price, fee, settled_at, payout, profit FROM paper_orders").fetchall()}


BOOKS = {SHORT: (100.0, 100.2), LONG: (50.0, 50.1)}


def test_a_high_funding_swap_opens_a_short_and_a_negative_one_a_long(conn):
    rates(conn, SHORT, (-1, 0.0005))
    rates(conn, LONG, (-1, -0.0004))
    out = run(conn, BOOKS, NOW)
    assert out["accounts"][ACCOUNT]["entries"] == 2
    got = positions(conn)
    side, n, price, fee, *_ = got[f"okx:{SHORT}"]
    assert (side, price) == ("short", 100.0) and n == pytest.approx(10) and fee == pytest.approx(0.5)
    side, n, price, fee, *_ = got[f"okx:{LONG}"]
    assert (side, price) == ("long", 50.1) and n == pytest.approx(1000 / 50.1)
    assert conn.execute("SELECT venue FROM paper_accounts WHERE account = %s", (ACCOUNT,)).fetchone() == ("futures",)
    assert conn.execute("SELECT count(*) FROM decisions WHERE league = 'futures' AND action = 'enter'").fetchone()[0] == 2


def test_a_position_marks_at_its_exit_price_with_a_short_gaining_when_the_price_falls(conn):
    rates(conn, SHORT, (-1, 0.0005))
    run(conn, BOOKS, NOW)
    run(conn, {SHORT: (89.8, 90.0)}, NOW + dt.timedelta(hours=1))  # held, marks: a short buys back at the ask
    eq, n = conn.execute("SELECT equity, open_positions FROM paper_accounts WHERE account = %s", (ACCOUNT,)).fetchone()
    assert n == 1 and eq == pytest.approx(10000 + 10 * (100.0 - 90.0) - 0.5)


def test_a_long_and_a_short_settle_after_the_hold_at_the_opposite_side(conn):
    rates(conn, SHORT, (-1, 0.0005))
    rates(conn, LONG, (-1, -0.0004))
    run(conn, BOOKS, NOW)
    out = run(conn, {SHORT: (95.0, 95.2), LONG: (52.0, 52.1)}, LATER)
    assert out["accounts"][ACCOUNT]["exits"] == 2
    short = positions(conn)[f"okx:{SHORT}"]
    long = positions(conn)[f"okx:{LONG}"]
    assert short[4] is not None and short[5] == 95.2 and long[5] == 52.0
    n = 10
    assert short[6] == pytest.approx(n * (100.0 - 95.2) - 0.5 - n * 95.2 * 5 / 1e4)
    m = 1000 / 50.1
    assert long[6] == pytest.approx(m * (52.0 - 50.1) - 0.5 - m * 52.0 * 5 / 1e4)
    eq, realized, n_open = conn.execute("SELECT equity, realized, open_positions FROM paper_accounts WHERE account = %s",
                                        (ACCOUNT,)).fetchone()
    assert n_open == 0 and realized == pytest.approx(short[6] + long[6]) and eq == pytest.approx(10000 + realized)


def test_funding_lowers_a_long_and_raises_a_short_by_the_recorded_rate(conn):
    rates(conn, SHORT, (-1, 0.0005))
    rates(conn, LONG, (-1, -0.0004))
    run(conn, BOOKS, NOW)
    # two settlements while held (the one before the entry is not charged): a short receives, a long pays
    rates(conn, SHORT, (8, 0.0005), (16, 0.0003))
    rates(conn, LONG, (8, -0.0004), (16, 0.0002))
    run(conn, BOOKS, LATER)  # flat prices: the profit is the fees and the funding
    got = positions(conn)
    short_fees = 0.5 + 10 * 100.2 * 5 / 1e4
    long_fees = 0.5 + (1000 / 50.1) * 50.0 * 5 / 1e4
    assert got[f"okx:{SHORT}"][6] == pytest.approx(10 * (100.0 - 100.2) - short_fees + 1000 * (0.0005 + 0.0003))
    assert got[f"okx:{LONG}"][6] == pytest.approx((1000 / 50.1) * (50.0 - 50.1) - long_fees - 1000 * (-0.0004 + 0.0002))
    d = conn.execute("SELECT sizing FROM decisions WHERE action = 'exit' AND instrument = %s", (f"okx:{SHORT}",)).fetchone()
    assert d[0]["funding"] == pytest.approx(-1000 * 0.0008)


def test_a_stale_funding_rate_or_a_stopped_account_takes_no_entry(conn):
    rates(conn, SHORT, (-20, 0.0005))
    run(conn, BOOKS, NOW)
    assert not positions(conn)
    rates(conn, SHORT, (-1, 0.0005))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, stopped_at) VALUES (%s, now(), now(), now())", (ACCOUNT,))
    out = run(conn, BOOKS, NOW)
    assert out["stopped"] == [ACCOUNT] and not positions(conn)


def test_no_fresh_quote_is_idle_and_nothing_trades(conn):
    rates(conn, SHORT, (-1, 0.0005))
    run(conn, BOOKS, NOW, quoted=NOW - dt.timedelta(hours=1))
    assert not positions(conn)


def test_the_module_has_no_order_call():
    import inspect
    assert "create_order" not in inspect.getsource(futures)
