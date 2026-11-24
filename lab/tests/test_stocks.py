"""The stock league (lab/stocks/paper.py) on a fake Alpaca data client: every entry and exit is a traced decision."""

import copy
import datetime as dt
from datetime import date as D

import pytest

from lab.money import broker, watcher
from lab.inputs import calls
from lab.stocks import paper as stocks
from lab.market import Span, bps_fee

ET = dt.timezone(dt.timedelta(hours=-5))
MONDAY = dt.datetime(2026, 11, 16, 10, tzinfo=ET)  # in session
LATER = MONDAY + dt.timedelta(hours=1)
SATURDAY = dt.datetime(2026, 11, 14, 12, tzinfo=ET)
CCFG = {"settle_hours": 2, "cost_bps": 2}
SESSIONS = [D(2026, 11, d) for d in (3, 4, 5, 6, 9, 10, 11, 12, 13)]
# SPY rises (above its 3-session average); TLT fell 6% over the last 2 sessions; GLD is flat.
CLOSES = {"SPY": [100, 101, 102, 103, 104, 105, 106, 107, 108], "TLT": [100] * 7 + [97, 94], "GLD": [50] * 9}


def cfg(**over):
    """The shipped config with the test universe; a knob of the [paper] table is overridden there, any other at the top."""
    c = copy.deepcopy(stocks.load_config())
    c["paper"] |= {"stake": 1000.0, "capital": 10000.0, "max_open": 8, "near_misses": 2}
    c.update(universe=["SPY", "TLT", "GLD"],
             families={"trend": {"sma": 3}, "reversion": {"lookback": 2, "drop": 0.05, "exit_sma": 2, "max_days": 3}})
    for k, v in over.items():
        (c["paper"] if k in c["paper"] else c)[k] = v
    return c


TREND = stocks.account_of("trend", stocks.variant_of({"sma": 3}))
REV = stocks.account_of("reversion", stocks.variant_of({"lookback": 2, "drop": 0.05, "exit_sma": 2, "max_days": 3}))


class Data:
    """AlpacaData's two reads, from fixed bars and quotes; records each call."""

    def __init__(self, closes=CLOSES, quotes=None, at=MONDAY):
        self.bars = {s: list(zip(SESSIONS, map(float, v))) for s, v in closes.items()}
        self.q = quotes if quotes is not None else {s: (v[-1] - 0.01, v[-1] + 0.01) for s, v in closes.items()}
        self.at, self.calls = at, []

    def closes(self, symbols, start, end=None):
        self.calls.append(("closes", tuple(symbols), end))
        return {s: self.bars.get(s, []) for s in symbols}

    def quotes(self, symbols, feed):
        self.calls.append(("quotes", tuple(symbols), feed))
        return {s: {"bid": b, "ask": a, "bid_size": 100.0, "ask_size": 100.0, "at": self.at - dt.timedelta(minutes=1)}
                for s, (b, a) in self.q.items() if s in symbols}


def run(conn, data=None, now=MONDAY, **over):
    return stocks.run_pass(conn, cfg(**over), data or Data(at=now), now.astimezone(dt.timezone.utc), CCFG)


def positions(conn):
    return conn.execute("SELECT account, market_id, contracts, price, fee, settled_at, payout, profit, "
                        "exit_reason FROM paper_orders ORDER BY id").fetchall()


def test_a_pass_enters_by_each_rule_and_every_fill_has_its_decision(conn):
    out = run(conn)
    assert out["open"] and out["accounts"][TREND]["entries"] == 1 and out["accounts"][REV]["entries"] == 1
    got = {(a, t): (shares, price, cost) for a, t, shares, price, cost, *_ in positions(conn)}
    assert set(got) == {(TREND, "SPY"), (REV, "TLT")}
    shares, price, cost = got[(TREND, "SPY")]
    assert price == pytest.approx(108.01) and shares == pytest.approx(1000 / 108.01) and cost == pytest.approx(0.2)
    rows = conn.execute("SELECT d.account, d.instrument, d.rule, d.checks, d.inputs, d.sizing FROM decisions d "
                        "JOIN fills f ON f.source = d.fill_source AND f.fill_id = d.fill_id "
                        "WHERE d.league = 'stocks' AND d.action = 'enter' ORDER BY d.id").fetchall()
    assert [(a, t) for a, t, *_ in rows] == [(REV, "TLT"), (TREND, "SPY")]
    _, _, rule, checks, inputs, sizing = rows[1]
    assert rule == "trend: hold a ticker while its daily close is above its 3-session average"
    assert [c["name"] for c in checks] == ["history", "close above average", "quote fresh", "quote valid", "spread"]
    assert all(c["pass"] for c in checks) and checks[1]["threshold"] == pytest.approx(107)
    assert {i["name"]: i["source"] for i in inputs} == {
        "daily_close": "alpaca daily bars", "stock_bid": "alpaca latest quote", "stock_ask": "alpaca latest quote"}
    assert sizing["stake"] == 1000 and sizing["cash"] == 10000 and sizing["cost_bps"] == 2
    # The funnel counts every ticker each account looked at, by where it stopped.
    funnel = dict(conn.execute("SELECT reason, n FROM decision_funnel WHERE account = %s", (TREND,)).fetchall())
    assert funnel == {"ordered": 1, "close above average": 2} and sum(funnel.values()) == 3
    fill = conn.execute("SELECT venue, strategy, is_open, stake FROM fills WHERE source = 'paper' AND venue = 'alpaca' AND account = %s",
                        (TREND,)).fetchone()
    assert fill == ("alpaca", "trend", True, pytest.approx(1000.2))
    eq, realized, n = conn.execute("SELECT equity, realized, open_positions FROM paper_accounts WHERE account = %s",
                                   (TREND,)).fetchone()
    assert n == 1 and realized == 0 and eq == pytest.approx(10000 + shares * (107.99 - 108.01) - 0.2)


def test_a_weekend_pass_is_market_closed_reads_no_quote_and_fills_nothing(conn):
    data = Data(at=SATURDAY)
    out = run(conn, data, now=SATURDAY)
    assert not out["open"] and not positions(conn) and [c[0] for c in data.calls] == ["closes"]
    assert conn.execute("SELECT DISTINCT reason, n FROM decision_funnel").fetchall() == [("market_closed", 1)]
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM paper_accounts WHERE marked_at IS NOT NULL").fetchone()[0] == 2


def test_a_session_with_no_fresh_quote_is_market_closed(conn):
    out = run(conn, Data(at=MONDAY - dt.timedelta(hours=20)))  # a holiday: yesterday's quotes
    assert not out["open"] and not positions(conn)
    assert {r for (r,) in conn.execute("SELECT reason FROM decision_funnel")} == {"market_closed"}


def test_an_exit_sells_at_the_bid_and_records_why(conn):
    run(conn)
    fell = {**CLOSES, "SPY": CLOSES["SPY"][:-1] + [100]}
    data = Data(fell, quotes={"SPY": (99.99, 100.01), "TLT": (93.99, 94.01), "GLD": (49.99, 50.01)}, at=LATER)
    out = run(conn, data, now=LATER)
    assert out["accounts"][TREND]["exits"] == 1
    _, _, shares, entry, cost, exited, price, profit, reason = next(p for p in positions(conn) if p[0] == TREND)
    assert exited is not None and price == pytest.approx(99.99) and reason == "close below average"
    exit_cost = shares * 99.99 * 2 / 1e4
    assert profit == pytest.approx(shares * (99.99 - entry) - cost - exit_cost)
    d = conn.execute("SELECT checks, sizing, fill_source FROM decisions WHERE action = 'exit'").fetchone()
    assert d[0][0]["name"] == "close below average" and d[0][0]["pass"] and d[2] is None
    assert d[1]["profit"] == pytest.approx(profit)
    eq, realized = conn.execute("SELECT equity, realized FROM paper_accounts WHERE account = %s", (TREND,)).fetchone()
    assert realized == pytest.approx(profit) and eq == pytest.approx(10000 + profit)
    assert conn.execute("SELECT is_open, profit_abs FROM fills WHERE source = 'paper' AND venue = 'alpaca' AND account = %s",
                        (TREND,)).fetchone() == (False, pytest.approx(profit))


def test_a_stopped_account_takes_no_entry_but_still_exits(conn):
    run(conn)
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, stopped_at) VALUES (%s, now(), now(), now())", (TREND,))
    fell = {**CLOSES, "SPY": CLOSES["SPY"][:-1] + [100], "GLD": [50] * 8 + [60]}  # GLD would now enter
    out = run(conn, Data(fell, at=LATER), now=LATER)
    assert out["stopped"] == [TREND] and out["accounts"][TREND] == {"exits": 1, "entries": 0, "no_cash": 0, "capped": 0}
    assert [t for a, t, *_ in positions(conn) if a == TREND] == ["SPY"]


def test_near_misses_are_capped_and_cash_and_max_open_hold(conn):
    flat = {s: [50] * 9 for s in ("SPY", "TLT", "GLD")}
    run(conn, Data(flat), near_misses=1)  # flat: every ticker fails one check, close above average
    assert conn.execute("SELECT count(*) FROM decisions WHERE action = 'near_miss' AND account = %s",
                        (TREND,)).fetchone()[0] == 1
    conn.execute("TRUNCATE decisions, decision_funnel, paper_orders, paper_accounts")
    rising = {s: list(range(100, 109)) for s in ("SPY", "TLT", "GLD")}
    out = run(conn, Data(rising), max_open=1)
    assert out["accounts"][TREND] == {"exits": 0, "entries": 1, "no_cash": 0, "capped": 2}
    conn.execute("TRUNCATE decisions, decision_funnel, paper_orders, paper_accounts")
    out = run(conn, Data(rising), capital=1500.0)
    assert out["accounts"][TREND] == {"exits": 0, "entries": 1, "no_cash": 2, "capped": 0}


def test_a_variant_dropped_from_the_toml_is_retired_and_takes_no_entry(conn):
    run(conn)
    out = run(conn, now=LATER, families={"trend": {"sma": 3}})
    assert out["retired"] == [REV]
    assert conn.execute("SELECT retired_at IS NOT NULL FROM paper_accounts WHERE account = %s", (REV,)).fetchone()[0]


def test_the_watcher_rows_a_stock_account_and_its_drill_stops_it(conn):
    run(conn)
    lim = {"stage": "Stage 1", "cap": 500.0, "hard_stop": 0.15}
    watcher.tick(conn, [], 0.05, lambda i, path, post=False: {}, dt.datetime.now(dt.timezone.utc), lim)
    assert conn.execute("SELECT state FROM watch WHERE bot = %s", (TREND,)).fetchone() == ("running",)
    watcher.drill(conn, TREND)
    watcher.tick(conn, [], 0.05, lambda i, path, post=False: {}, dt.datetime.now(dt.timezone.utc), lim)
    assert conn.execute("SELECT state FROM watch WHERE bot = %s", (TREND,)).fetchone() == ("paused",)
    assert TREND in run(conn, now=LATER)["stopped"]


class Latest:
    def __init__(self):
        self.calls = []

    def request(self, method, url, json, timeout, headers, params=None):
        self.calls.append((method, url, dict(params or {})))
        quotes = {"SPY": {"t": "2026-11-16T15:00:00.123456789Z", "bp": 600.1, "ap": 600.12, "bs": 3, "as": 2}}

        class Resp:
            status_code, text, headers = 200, "", {}

            def json(self):
                return {"quotes": quotes}
        return Resp()


def test_the_quote_read_is_a_get_to_the_data_host_on_the_feed_asked():
    s = Latest()
    c = broker.AlpacaData({"base_url": "https://data.alpaca.markets", "feed": "sip", "timeout_s": 5, "page_gap_s": 0,
                           "retries": 2}, "k", "s", s, sleep=lambda _: None)
    q = c.quotes(["SPY"], "iex")["SPY"]
    assert q["bid"] == 600.1 and q["ask"] == 600.12 and q["ask_size"] == 2
    assert q["at"] == dt.datetime(2026, 11, 16, 15, 0, 0, 123456, tzinfo=dt.timezone.utc)
    method, url, params = s.calls[0]
    assert method == "GET" and url == "https://data.alpaca.markets/v2/stocks/quotes/latest"
    assert params == {"symbols": "SPY", "feed": "iex"}


def test_the_rule_in_words_and_the_toml_families_load():
    league = stocks.league_of(stocks.load_config())
    assert league and {f for _, f, _ in league} == set(stocks.FAMILIES)
    assert stocks.rule_of("nope", {}) == "nope"
    assert "5-session fall of 3%" in stocks.rule_of("reversion", {"lookback": 5, "drop": 0.03, "exit_sma": 5,
                                                                   "max_days": 10})


def test_stocks_are_a_market_venue_and_history_over_alpaca_data():
    """AlpacaStocks implements lab/market.py Venue and History: one quote page for the universe, no resolutions or
    settled history, and daily closes as bars ending at the session close; any other bar period is refused."""
    data = Data()
    venue = stocks.AlpacaStocks(data, ["SPY", "TLT"], "iex")
    [page] = list(venue.open_markets())
    q = {x.market_id: x for x in page}["SPY"]
    assert (q.outcome, q.market_type, q.ts) == ("long", "stock", data.at - dt.timedelta(minutes=1))
    assert (q.bid, q.ask) == (pytest.approx(107.99), pytest.approx(108.01))
    assert data.calls == [("quotes", ("SPY", "TLT"), "iex")]
    assert list(venue.resolutions(MONDAY)) == [] and list(venue.settled(None)) == []
    span = Span("SPY", None, dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc), MONDAY)
    bars = venue.bars(span, 86400)
    assert [b.close for b in bars] == [float(c) for c in CLOSES["SPY"]]
    assert bars[0].ts == calls.close_at(SESSIONS[0]) and {b.period_s for b in bars} == {86400}
    with pytest.raises(ValueError):
        venue.bars(span, 3600)


def test_a_stock_side_costs_its_basis_points_of_notional():
    assert bps_fee(2, 1000.0) == pytest.approx(0.2) and bps_fee(2) == pytest.approx(0.0002) and bps_fee(0, 50.0) == 0
