import datetime as dt

from lab import futures
from lab.util import utc

UTC = dt.timezone.utc
DAY = futures.DAY_MS
H8 = DAY // 3


class FakeVenue:
    """Funding every 8 hours from `listed`; `ignores_until` acts like okx, which returns everything after since."""

    def __init__(self, listed: int, now: int, ignores_until=False, broken=()):
        self.listed, self.now, self.ignores_until, self.broken = listed, now, ignores_until, set(broken)
        self.calls: list[tuple] = []

    def swaps(self):
        return {"BTC/USDT:USDT", "ETH/USDT:USDT", "BAD/USDT:USDT"}

    def funding(self, symbol, since, until):
        self.calls.append(("funding", symbol, since, until))
        if symbol in self.broken:
            raise RuntimeError("throttled")
        end = self.now if self.ignores_until else until
        first = max(self.listed, -(-since // H8) * H8)
        return [(t, 0.0001) for t in range(first, end + 1, H8) if t <= self.now][:100]

    def open_interest(self, symbol, since):
        self.calls.append(("oi", symbol, since))
        return [(self.now - k * DAY, 10.0 + k, None) for k in range(3)]


def stored(conn, table):
    return conn.execute(f"SELECT pair, count(*), min(ts), max(ts) FROM {table} GROUP BY pair ORDER BY pair").fetchall()


def test_funding_walks_forward_in_windows_and_resumes_from_the_newest_row(conn, monkeypatch):
    start = dt.datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(futures, "FUNDING_FROM", start)
    now = futures.ms(start) + 70 * DAY
    v = FakeVenue(listed=futures.ms(start) + 10 * DAY, now=now)

    assert futures.collect(v, conn, "kucoin", ["BTC/USDT", "SOL/USDT"], now)
    (pair, n, first, last), = stored(conn, "funding_rates")
    assert pair == "BTC/USDT:USDT" and n == 60 * 3 + 1  # SOL has no swap listed, so it is skipped
    assert first == start + dt.timedelta(days=10) and last == utc(now)
    assert sum(c[0] == "funding" for c in v.calls) == 3  # 70 days in 30-day windows

    v.calls.clear()
    v.now = now + DAY
    assert futures.collect(v, conn, "kucoin", ["BTC/USDT"], now + DAY)
    assert [c[2] for c in v.calls if c[0] == "funding"] == [now + 1]  # only what is new
    assert stored(conn, "funding_rates")[0][1] == 61 * 3 + 1


def test_a_venue_that_ignores_until_is_fetched_once(conn, monkeypatch):
    start = dt.datetime(2024, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(futures, "FUNDING_FROM", start)
    now = futures.ms(start) + 700 * DAY
    v = FakeVenue(listed=now - 30 * DAY, now=now, ignores_until=True)
    assert futures.collect(v, conn, "okx", ["ETH/USDT"], now)
    calls = [c for c in v.calls if c[0] == "funding"]
    assert stored(conn, "funding_rates")[0][1] == 91
    assert len(calls) < 700 // 30  # it jumped to the newest row instead of walking every empty window


def test_open_interest_overlaps_without_duplicates_and_only_kucoin_gets_a_since(conn):
    now = futures.ms(dt.datetime(2026, 11, 5, tzinfo=UTC))
    for ex in ("okx", "kucoin", "kucoin"):
        v = FakeVenue(listed=now, now=now)
        assert futures.collect(v, conn, ex, ["BTC/USDT"], now)
        since = [c[2] for c in v.calls if c[0] == "oi"]
        assert (since == [None]) == (ex == "okx")
    rows = conn.execute("SELECT exchange, count(*) FROM open_interest GROUP BY exchange ORDER BY exchange").fetchall()
    assert rows == [("kucoin", 3), ("okx", 3)]


def test_one_failing_swap_is_reported_and_the_rest_are_kept(conn, monkeypatch):
    now = futures.ms(dt.datetime(2026, 11, 5, tzinfo=UTC))
    monkeypatch.setattr(futures, "FUNDING_FROM", utc(now - 5 * DAY))
    v = FakeVenue(listed=now - 5 * DAY, now=now, broken={"BAD/USDT:USDT"})
    assert not futures.collect(v, conn, "okx", ["BAD/USDT", "BTC/USDT"], now)
    assert [r[0] for r in stored(conn, "funding_rates")] == ["BTC/USDT:USDT"]


class Throttled(Exception):
    pass


class FlakyExchange:
    def __init__(self, fails: int):
        self.fails, self.calls = fails, []

    def fetch_open_interest_history(self, symbol, timeframe, since=None, params=None):
        self.calls.append((timeframe, params))
        if self.fails:
            self.fails -= 1
            raise Throttled("50011 Too Many Requests")
        return [{"timestamp": 1, "openInterestAmount": 2.0, "openInterestValue": 3.0}]


def ccxt_venue(fails: int, exchange="okx"):
    v, waits = object.__new__(futures.Ccxt), []
    v.exchange, v.ex, v.throttled, v.sleep = exchange, FlakyExchange(fails), Throttled, waits.append
    return v, waits


def test_okx_open_interest_asks_for_the_utc_day_like_kucoin():
    okx, _ = ccxt_venue(fails=0)
    okx.open_interest("SOL/USDT:USDT", None)
    kucoin, _ = ccxt_venue(fails=0, exchange="kucoin")
    kucoin.open_interest("SOL/USDT:USDT", 1)
    assert okx.ex.calls == [("1d", {"paginate": False, "period": "1Dutc"})]  # 1D would open at 16:00 UTC
    assert kucoin.ex.calls == [("1d", {"paginate": True})]


def test_a_rate_limited_call_waits_and_retries():
    v, waits = ccxt_venue(fails=2)
    assert v.open_interest("SOL/USDT:USDT", None) == [(1, 2.0, 3.0)]
    assert waits == list(futures.RETRY_WAITS[:2])


def test_a_call_still_throttled_after_every_retry_raises():
    v, waits = ccxt_venue(fails=len(futures.RETRY_WAITS) + 1)
    try:
        v.open_interest("SOL/USDT:USDT", None)
    except Throttled:
        pass
    else:
        raise AssertionError("expected the last throttle to surface")
    assert waits == list(futures.RETRY_WAITS)


def test_migration_061_drops_only_the_okx_utc8_rows_the_utc_refetch_replaces(conn):
    today = dt.datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [("okx", today - dt.timedelta(days=d, hours=8)) for d in (0, 98, 99, 100)]  # 16:00 UTC, okx's 1D
    rows += [("okx", today - dt.timedelta(days=d)) for d in (0, 99)] + [("kucoin", today - dt.timedelta(days=1))]
    conn.cursor().executemany("INSERT INTO open_interest (exchange, pair, ts, value_usd) VALUES (%s, 'BTC/USDT:USDT', %s, 1)",
                              rows)
    conn.execute((futures.Path(futures.__file__).parent / "migrations" / "061_okx_open_interest_utc_day.sql").read_text())
    kept = conn.execute("SELECT exchange, ts FROM open_interest ORDER BY exchange, ts").fetchall()
    assert kept == [("kucoin", today - dt.timedelta(days=1)),
                    ("okx", today - dt.timedelta(days=100, hours=8)), ("okx", today - dt.timedelta(days=99, hours=8)),
                    ("okx", today - dt.timedelta(days=99)), ("okx", today)]
