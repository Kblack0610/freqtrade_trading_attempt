"""/accounts lists every venue in web/venues.toml once: connected, error, stale or missing, and the person's step."""

import datetime as dt

from test_web import seed_markets, signed_in, site  # noqa: F401 - fixtures

from web import venues

NOW = dt.datetime(2026, 11, 12, 18, tzinfo=dt.timezone.utc)
H = dt.timedelta(hours=1)


def fake(**found):
    """query(sql) answering each source's SQL with the rows given for it."""
    by_sql = {venues.SOURCES[s]: rows for s, rows in found.items()}
    return lambda sql: by_sql.get(sql, [])


def by_name(rows):
    return {r["name"]: r for r in rows}


def test_every_venue_is_one_row_and_a_venue_with_no_read_is_missing_with_the_persons_step():
    rows = by_name(venues.rows(fake(), NOW))
    assert len(rows) == len(venues.load()) and {"Alpaca", "Kraken", "Fidelity (SnapTrade)", "Trezor", "Tastytrade"} <= set(rows)
    assert all(r["status"] == "missing" for r in rows.values())
    assert "SnapTrade Connection Portal" in rows["Fidelity (SnapTrade)"]["human"]


def test_a_venue_is_connected_only_when_every_check_is_fresh_and_clean():
    rows = by_name(venues.rows(fake(
        broker=[{"key": "alpaca_paper", "at": NOW - H, "error": None},
                {"key": "kraken", "at": NOW - 5 * H, "error": None},
                {"key": "fidelity_individual_1234", "at": NOW - H, "error": None},
                {"key": "fidelity_roth_ira_5678", "at": NOW - H, "error": "snaptrade: no total balance"}],
        drill=[{"key": "alpaca_paper", "at": NOW - 2 * H, "error": "RuntimeError: order o1 is new after the cancel"},
               {"key": "kalshi_demo", "at": NOW - 20 * H, "error": None}],
        paper=[{"key": "kalshi", "at": NOW - dt.timedelta(minutes=5), "error": None}]), NOW))
    assert rows["Kalshi"]["status"] == "connected"
    assert rows["Alpaca"]["status"] == "error" and "after the cancel" in rows["Alpaca"]["checks"][1]["error"]
    assert rows["Kraken"]["status"] == "stale"  # hourly read, 5 h old
    assert rows["Fidelity (SnapTrade)"]["status"] == "error"  # the worst of its accounts
    assert rows["Tastytrade"]["status"] == "missing" and rows["Tastytrade"]["checks"] == []


def test_accounts_page_shows_the_venue_table(conn, signed_in):  # noqa: F811
    seed_markets(conn)  # an Alpaca paper read and a Fidelity read, among others
    body = signed_in[1].get("/accounts").text
    table = body.split("<h2>Venues</h2>")[1].split("<h2>Broker accounts</h2>")[0]
    for name in (v["name"] for v in venues.load()):
        assert name in table, name
    assert "Open a Tastytrade account" in table and "Kraken" in table
