import datetime as dt

import pandas as pd
import pytest

from lab.crypto import backfill, listings

UTC = dt.timezone.utc


def ts(*a):
    return dt.datetime(*a, tzinfo=UTC)


def ms(d):
    return int(d.timestamp() * 1000)


SRM = ("SRMUSDT", ts(2022, 9, 2), ts(2022, 12, 21, 23))
BTC = ("BTCUSDT", ts(2019, 9, 1), ts(2026, 11, 27))


class FakeApi:
    """exchangeInfo plus 1h klines for the symbols above; a USD pair and a non-listed status are in the info."""

    def __init__(self):
        self.calls = []

    def get(self, path, params=None, label=None):
        self.calls.append((path, params))
        if path == "/exchangeInfo":
            return {"symbols": [
                {"symbol": "SRMUSDT", "baseAsset": "SRM", "quoteAsset": "USDT", "status": "BREAK"},
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BTCUSD", "baseAsset": "BTC", "quoteAsset": "USD", "status": "TRADING"},
                {"symbol": "XUSDT", "baseAsset": "X", "quoteAsset": "USDT", "status": "HALT"},
                {"symbol": "NOKLINE", "baseAsset": "NO", "quoteAsset": "USDT", "status": "BREAK"}]}
        _, first, last = {"SRMUSDT": SRM, "BTCUSDT": BTC}.get(params["symbol"], (None, None, None))
        if first is None:
            return []
        if "startTime" in params and params["limit"] == 1 and params["startTime"] == 0:
            return [[ms(first)]]
        if params["limit"] == 1:
            return [[ms(last)]]
        step = 3600 * 1000
        n = min(params["limit"], (params["endTime"] - params["startTime"]) // step + 1)
        return [[params["startTime"] + i * step, "1", "1", "1", "1", "1"] for i in range(n)]


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(listings, "API", fake)
    return fake


def test_months_span_the_window():
    assert listings.months(SRM[1], SRM[2]) == [dt.date(2022, m, 1) for m in (9, 10, 11, 12)]


def test_symbols_keep_usdt_trading_and_break(api):
    assert [s["pair"] for s in listings.symbols()] == ["SRM/USDT", "BTC/USDT", "NO/USDT"]


def test_refresh_puts_srm_in_2022_10_and_not_2023_01(conn, api):
    found, failed = listings.refresh(conn)
    assert set(found) == {"SRM/USDT", "BTC/USDT"}, "a symbol with no candles has no window"
    assert "SRM/USDT" in listings.listed_in(conn, dt.date(2022, 10, 15))
    assert "SRM/USDT" not in listings.listed_in(conn, dt.date(2023, 1, 1))
    assert "BTC/USDT" in listings.listed_in(conn, dt.date(2023, 1, 1))
    first, last = conn.execute("SELECT first_ts, last_ts FROM pair_windows WHERE pair = 'SRM/USDT'").fetchone()
    assert (first, last) == SRM[1:]


def test_delisted_candles_run_over_the_window_and_are_not_refetched(conn, api, tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "RESULTS", tmp_path)
    found, failed = listings.refresh(conn)
    assert listings.backfill_delisted(conn, found, ["1h"])
    df = pd.read_feather(backfill.series_file("binanceus", "SRM/USDT", "1h"))
    assert df["date"].min() == SRM[1] and df["date"].max() == SRM[2]
    assert not backfill.series_file("binanceus", "BTC/USDT", "1h").exists(), "a live pair is the backfill's"
    cov = conn.execute("SELECT first_ts, last_ts, oldest_reached FROM data_coverage WHERE pair = 'SRM/USDT'").fetchone()
    assert cov == (SRM[1], SRM[2], True)

    api.calls.clear()
    assert listings.backfill_delisted(conn, found, ["1h"])
    assert not [c for c in api.calls if c[1].get("limit") == listings.PAGE], "complete on disk: nothing fetched"


def test_a_timeframe_floor_skips_a_series_that_ended_before_it(conn, api, tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "RESULTS", tmp_path)
    monkeypatch.setattr(backfill, "PREPEND_FLOORS", {"1h": "20240101"})
    assert listings.backfill_delisted(conn, listings.refresh(conn)[0], ["1h"])
    assert not backfill.series_file("binanceus", "SRM/USDT", "1h").exists()


def test_an_unreadable_symbol_keeps_its_old_rows(conn, api, monkeypatch):
    listings.refresh(conn)
    real = listings.window
    monkeypatch.setattr(listings, "window", lambda sym: (_ for _ in ()).throw(OSError("down")) if sym == "SRMUSDT" else real(sym))
    found, failed = listings.refresh(conn)
    assert failed == ["SRM/USDT"] and set(found) == {"BTC/USDT"}
    assert "SRM/USDT" in listings.listed_in(conn, dt.date(2022, 10, 15)), "its months were not wiped"
