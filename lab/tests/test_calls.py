"""Calls: the ledger refuses edits, the scorer's short P&L on a fixed series, the data client refuses anything but a
GET to its host, and the nightly step scores what has printed and fails what is overdue."""

import argparse
import datetime as dt

import psycopg
import pytest

from lab import broker, calls

CFG = {"cost_bps": 2, "borrow_apr": 0.003, "settle_hours": 2, "slack_days": 4, "alpha": 0.05, "bound": 1.0}
DATA = {"base_url": "https://data.alpaca.markets", "feed": "sip", "timeout_s": 5, "page_gap_s": 0, "retries": 2}
D = dt.date
# Fri 2026-11-13 entry, then Mon..Fri of the next week
NVDA = [(D(2026, 11, 12), 166.19), (D(2026, 11, 13), 100.0), (D(2026, 11, 16), 90.0), (D(2026, 11, 17), 95.0),
        (D(2026, 11, 18), 80.0), (D(2026, 11, 19), 85.0), (D(2026, 11, 20), 110.0)]
SMH = [(d, 200.0 + i) for i, (d, _) in enumerate(NVDA)]


def at(day: dt.date, hour: int = 22) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), calls.ET)


def test_a_short_earns_the_fall_less_borrow_and_costs():
    s = calls.score("short", D(2026, 11, 13), 1, NVDA, SMH, CFG)
    # 3 calendar days Fri -> Mon of borrow at 0.3% a year (actual/360), and 2 bp each way
    assert s["exit_session"] == D(2026, 11, 16) and s["entry_close"] == 100.0 and s["exit_close"] == 90.0
    assert s["ret"] == pytest.approx(-0.10)
    assert s["pnl"] == pytest.approx(0.10 - 0.003 * 3 / 360 - 0.0004)
    assert s["excess"] == pytest.approx(-(-0.10 - (202 / 201 - 1)))
    five = calls.score("short", D(2026, 11, 13), 5, NVDA, SMH, CFG)
    assert five["exit_session"] == D(2026, 11, 20) and five["pnl"] == pytest.approx(-0.10 - 0.003 * 7 / 360 - 0.0004)
    long = calls.score("long", D(2026, 11, 13), 5, NVDA, SMH, CFG)
    assert long["pnl"] == pytest.approx(0.10 - 0.0004)  # a long pays no borrow


def test_no_score_until_the_exit_close_has_printed_and_settled():
    assert calls.score("short", D(2026, 11, 13), 20, NVDA, SMH, CFG) is None
    assert calls.score("short", D(2026, 11, 13), 1, NVDA, [], CFG) is None  # no benchmark close, no score
    # Monday's bar read at 17:00 ET, an hour after the close, is not final yet
    assert calls.final(NVDA, at(D(2026, 11, 16), 17), 2)[-1][0] == D(2026, 11, 13)
    assert calls.final(NVDA, at(D(2026, 11, 16), 18), 2)[-1][0] == D(2026, 11, 16)


def test_the_next_entry_is_the_next_close_still_ahead():
    assert calls.next_entry(at(D(2026, 11, 13), 15)) == D(2026, 11, 13)
    assert calls.next_entry(at(D(2026, 11, 13), 16)) == D(2026, 11, 16)  # Friday's close printed: Monday
    assert calls.next_entry(at(D(2026, 11, 14), 10)) == D(2026, 11, 16)


class Bars:
    """data.alpaca.markets /v2/stocks/bars in two pages; records every request."""

    def __init__(self, series):
        self.series, self.calls = series, []

    def request(self, method, url, json, timeout, headers, params=None):
        self.calls.append((method, url, dict(params or {}), headers))
        assert url == "https://data.alpaca.markets/v2/stocks/bars" and params["feed"] == "sip"
        assert params["adjustment"] == "all" and headers["APCA-API-KEY-ID"] == "k"
        bars = {s: [{"t": f"{d}T05:00:00Z", "c": c} for d, c in v if d >= D.fromisoformat(params["start"])]
                for s, v in self.series.items() if s in params["symbols"].split(",")}
        first = "page_token" not in params
        page = {s: (v[:2] if first else v[2:]) for s, v in bars.items()}
        return Resp(200, {"bars": page, "next_page_token": "p2" if first else None})


class Resp:
    def __init__(self, status, body):
        self.status_code, self.body, self.text, self.headers = status, body, str(body), {}

    def json(self):
        return self.body


def data(series=None):
    s = Bars(series or {"NVDA": NVDA, "SMH": SMH})
    return broker.AlpacaData(DATA, "k", "s", s, sleep=lambda _: None), s


def test_the_data_client_reads_every_page_and_refuses_anything_but_a_get_to_its_host():
    c, s = data()
    out = c.closes(["NVDA", "SMH"], D(2026, 11, 13))
    assert out["NVDA"] == NVDA[1:] and out["SMH"] == SMH[1:] and len(s.calls) == 2
    for method in ("POST", "DELETE", "PATCH", "PUT"):
        with pytest.raises(ValueError, match="GET only"):
            c.call(method, "/v2/orders")
    assert len(s.calls) == 2  # refused before a request left
    for url in ("https://paper-api.alpaca.markets", "https://api.alpaca.markets", "http://data.alpaca.markets"):
        with pytest.raises(broker.WrongHost):
            broker.AlpacaData({**DATA, "base_url": url}, "k", "s")


def call(conn, **kw):
    row = {"who": "human", "author": "kenneth", "thesis": "t", "ticker": "NVDA", "direction": "short",
           "instrument": "equity", "entry_session": D(2026, 11, 13), "horizons": [1, 5, 20], "benchmark": "SMH",
           "source": None, **kw}
    return conn.execute(
        "INSERT INTO calls (who, author, thesis, ticker, direction, instrument, entry_session, horizons, benchmark, "
        "source) VALUES (%(who)s, %(author)s, %(thesis)s, %(ticker)s, %(direction)s, %(instrument)s, "
        "%(entry_session)s, %(horizons)s, %(benchmark)s, %(source)s) RETURNING id", row).fetchone()[0]


def test_the_ledger_refuses_update_and_delete(conn):
    cid = call(conn)
    for sql in ("UPDATE calls SET direction = 'long' WHERE id = %s", "DELETE FROM calls WHERE id = %s"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(sql, (cid,))
    calls.run(conn, data()[0], at(D(2026, 11, 20)), CFG)
    for sql in ("UPDATE call_scores SET pnl = 1 WHERE call_id = %s", "DELETE FROM call_scores WHERE call_id = %s"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(sql, (cid,))


def test_the_nightly_step_scores_what_printed_once_and_joins_the_fills_view(conn):
    cid = call(conn)
    out = calls.run(conn, data()[0], at(D(2026, 11, 20)), CFG)
    assert out == {"calls_scored": 2, "calls_pending": 1, "calls_unscored": 0, "calls_error": None}
    got = dict(conn.execute("SELECT horizon, pnl FROM call_scores WHERE call_id = %s", (cid,)).fetchall())
    assert sorted(got) == [1, 5] and got[1] == pytest.approx(0.10 - 0.003 * 3 / 360 - 0.0004)
    assert calls.run(conn, data()[0], at(D(2026, 11, 20)), CFG)["calls_scored"] == 0  # nothing new printed
    # the call is open in fills until its longest horizon (20) is scored, and calls stay out of strategy confidence
    assert conn.execute("SELECT source, account, venue, is_open FROM fills WHERE source = 'call'").fetchall() == \
        [("call", "human", "call", True)]
    g = calls.groups([{"who": "human", "instrument": "equity", "horizon": 1, "pnl": got[1]}], CFG)
    assert g[0]["n"] == 1 and g[0]["hit"] == 1 and g[0]["lo"] <= got[1] <= g[0]["hi"]


def test_an_overdue_call_with_no_prices_fails_the_step(conn, monkeypatch):
    call(conn, horizons=[1])
    empty = {"NVDA": [], "SMH": []}
    assert calls.run(conn, data(empty)[0], at(D(2026, 11, 16)), CFG)["calls_pending"] == 1  # not due yet
    out = calls.run(conn, data(empty)[0], at(D(2026, 11, 25)), CFG)
    assert out["calls_unscored"] == 1 and out["calls_scored"] == 0
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    out = calls.run(conn, None, at(D(2026, 11, 25)), CFG)
    assert out["calls_unscored"] == 1 and "ALPACA_PAPER_KEY_ID" in out["calls_error"]


def test_add_refuses_an_entry_whose_close_has_printed(conn):
    a = argparse.Namespace(who="agent", author="agent-5", thesis="t", ticker="nvda", direction="short",
                           instrument="equity", benchmark="smh", horizons="5,1", entry=D(2026, 11, 13), source=None)
    with pytest.raises(SystemExit, match="has printed"):
        calls.add(conn, a, at(D(2026, 11, 13), 17))
    cid = calls.add(conn, argparse.Namespace(**{**vars(a), "entry": None}), at(D(2026, 11, 13), 17))
    assert conn.execute("SELECT ticker, benchmark, entry_session, horizons FROM calls WHERE id = %s", (cid,)).fetchone() \
        == ("NVDA", "SMH", D(2026, 11, 16), [1, 5])
