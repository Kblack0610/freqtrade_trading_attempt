import copy
import datetime as dt

import pandas as pd
import pytest

from lab import watcher
from lab.markets import backtest, paper
from lab.markets.recorder import load_config

NOW = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
BAND = {"stake": 10, "max_spread": 0.05, "hours": 24, "bands": [{"buy": "no", "lo": 0.01, "hi": 0.10}]}


def cfg(**paper_over):
    c = copy.deepcopy(load_config())
    c["families"]["longshot"] = BAND
    c["paper"].update(paper_over)
    return c


ACCOUNT = paper.account_of("longshot", {"stake": 10, "max_spread": 0.05, "hours": 24, "buy": "no", "lo": 0.01,
                                        "hi": 0.10})


def seed(conn, sweep_age=dt.timedelta(minutes=5)):
    """A: a longshot closing in 12 h (enters, buys no at 0.95); B: the same price closing in 48 h (outside hours);
    C: a coin flip closing in 12 h (outside the band)."""
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'Kalshi', 'exchange')")
    for mid, close_h, bid, ask in (("A", 12, 0.05, 0.07), ("B", 48, 0.05, 0.07), ("C", 12, 0.49, 0.51)):
        conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, close_time, first_seen, "
                     "updated_at) VALUES ('kalshi', %s, %s, 'binary', 'active', %s, %s, %s)",
                     (mid, mid, NOW + dt.timedelta(hours=close_h), NOW, NOW))
        conn.execute("INSERT INTO outcomes (venue, market_id, outcome, bid, ask, bid_size, ask_size, quoted_at) "
                     "VALUES ('kalshi', %s, 'yes', %s, %s, 100, 100, %s)", (mid, bid, ask, NOW - dt.timedelta(minutes=7)))
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok) "
                 "VALUES ('kalshi', %s, %s, 3, 3, 3, 0, true)", (NOW - sweep_age, NOW - sweep_age))


def orders(conn):
    return conn.execute("SELECT account, market_id, side, contracts, price, fee, settled_at, payout, profit "
                        "FROM paper_orders ORDER BY id").fetchall()


def equity(conn, account=ACCOUNT):
    return conn.execute("SELECT equity, realized, open_positions, marked_at FROM paper_accounts WHERE account = %s",
                        (account,)).fetchone()


def test_an_account_enters_a_market_once_at_the_backtest_fill_and_marks_at_the_exit_price(conn):
    seed(conn)
    out = paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert out["fresh"] and out["accounts"][ACCOUNT] == {"orders": 1, "fills": 1, "no_cash": 0}
    # no at 1 - bid = 0.95; 10 contracts in a 10 dollar stake; Kalshi fee ceil(0.07 * 10 * 0.95 * 0.05) = 0.04
    [o] = orders(conn)
    assert o[:6] == (ACCOUNT, "A", "no", 10, pytest.approx(0.95), pytest.approx(0.04))
    # the no exits at 1 - ask = 0.93: 9.30 - 9.50 - 0.04
    assert equity(conn)[:3] == (pytest.approx(999.76), 0, 1) and equity(conn)[3] == NOW

    out = paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=5))
    assert out["accounts"][ACCOUNT]["orders"] == 0 and len(orders(conn)) == 1


def test_a_settled_fill_pays_what_the_backtest_pays(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    settled = NOW + dt.timedelta(hours=12)
    conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) "
                 "VALUES ('kalshi', 'A', 'no', 0, %s)", (settled,))
    out = paper.run_pass(conn, "kalshi", cfg(), settled + dt.timedelta(minutes=5))
    assert out["settled"] == 1
    *_, settled_at, pay, profit = orders(conn)[0]
    fills = pd.DataFrame([{"market_id": "A", "side": "no", "open_date": pd.Timestamp(NOW), "contracts": 10,
                           "price": 0.95, "fee": 0.04}])
    res = {"A": backtest.Resolution("A", "no", 0.0, settled)}
    expect = backtest.settle(fills, res, load_config()["backtest"]["fees"]["kalshi"]).iloc[0]
    assert (settled_at, pay, profit) == (settled, expect.close_rate, pytest.approx(expect.profit_abs))
    assert profit == pytest.approx(0.46)
    assert equity(conn)[:3] == (pytest.approx(1000.46), pytest.approx(0.46), 0)


def test_a_dead_recorder_means_no_entries(conn):
    seed(conn, sweep_age=dt.timedelta(minutes=20))
    out = paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert not out["fresh"] and out["book"] == 0 and orders(conn) == []
    assert equity(conn)[0] == 1000  # still marked, so the watcher sees the pass


def test_an_order_the_cash_cannot_pay_for_is_not_stored_and_asked_again(conn):
    seed(conn)
    out = paper.run_pass(conn, "kalshi", cfg(capital=5), NOW)
    assert out["accounts"][ACCOUNT] == {"orders": 0, "fills": 0, "no_cash": 1} and orders(conn) == []
    conn.execute("UPDATE paper_accounts SET capital = 1000")
    out = paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=5))
    assert out["accounts"][ACCOUNT]["fills"] == 1


def test_families_that_need_a_fit_or_a_key_are_refused():
    with pytest.raises(ValueError, match="cannot run in the paper league"):
        paper.accounts(cfg(families=["gap"]))


def test_the_watcher_drill_stops_a_paper_account_and_ending_it_starts_it_again(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    watcher.tick(conn, [], 0.03, None, NOW)
    w = conn.execute("SELECT dry_run, state, equity, seen_at FROM watch WHERE bot = %s", (ACCOUNT,)).fetchone()
    assert w == (True, "running", pytest.approx(999.76), NOW)

    watcher.drill(conn, ACCOUNT)
    later = NOW + dt.timedelta(minutes=1)
    watcher.tick(conn, [], 0.03, None, later)
    assert conn.execute("SELECT state, stopped_at, breached_at FROM watch WHERE bot = %s",
                        (ACCOUNT,)).fetchone() == ("paused", later, later)
    # a new market qualifies, but the stopped account takes no entry; its marks go on
    conn.execute("UPDATE outcomes SET bid = 0.05, ask = 0.07 WHERE market_id = 'C'")
    out = paper.run_pass(conn, "kalshi", cfg(), later + dt.timedelta(minutes=1))
    assert out["stopped"] == [ACCOUNT] and ACCOUNT not in out["accounts"] and len(orders(conn)) == 1
    assert equity(conn)[3] == later + dt.timedelta(minutes=1)

    watcher.drill_end(conn, ACCOUNT, [], None)  # no API to call: fetch is never touched
    watcher.tick(conn, [], 0.03, None, later + dt.timedelta(minutes=2))
    out = paper.run_pass(conn, "kalshi", cfg(), later + dt.timedelta(minutes=3))
    assert out["accounts"][ACCOUNT]["fills"] == 1 and [o[1] for o in orders(conn)] == ["A", "C"]


def test_a_paper_loss_past_the_day_limit_breaches_without_stopping(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    watcher.tick(conn, [], 0.03, None, NOW)
    conn.execute("UPDATE paper_accounts SET equity = 960, marked_at = %s", (NOW + dt.timedelta(minutes=5),))
    watcher.tick(conn, [], 0.03, None, NOW + dt.timedelta(minutes=6))
    breached, stopped, seen = conn.execute("SELECT breached_at, stopped_at, seen_at FROM watch WHERE bot = %s",
                                           (ACCOUNT,)).fetchone()
    assert breached is not None and stopped is None and seen == NOW + dt.timedelta(minutes=5)
