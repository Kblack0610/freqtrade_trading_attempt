import datetime as dt
import io
import zipfile

import pandas as pd
import pytest

import archive
import backfill

UTC = dt.timezone.utc
LISTED = dt.datetime(2026, 1, 1, tzinfo=UTC)
NOW = dt.datetime(2026, 1, 3, tzinfo=UTC)


def candles(start: dt.datetime, end: dt.datetime, freq: str, drop: int = 0) -> pd.DataFrame:
    dates = pd.date_range(start, end, freq=freq, tz=UTC, inclusive="left")
    if drop:  # a hole of `drop` candles in the middle
        mid = len(dates) // 2
        dates = dates.delete(slice(mid, mid + drop))
    return pd.DataFrame({"date": dates, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})


class FakeExchange:
    """Stands in for freqtrade download-data: every pair is listed at LISTED and trades up to NOW."""

    def __init__(self, supported=("1h", "1d"), fail=False, zipped=None):
        self.supported = list(supported)
        self.zipped = zipped
        self.opened = 0
        self.fail = fail
        self.calls: list[list[str]] = []
        self.now = NOW

    def recent(self, exchange, pair, timeframe):
        """The 6 newest candles up to self.now, the last one still forming, like Kraken's 720."""
        step = pd.Timedelta(timeframe)
        dates = pd.date_range(end=pd.Timestamp(self.now).floor(step), periods=6, freq=step)
        return [[int(d.timestamp() * 1000), 1.0, 1.0, 1.0, 1.0, 1.0] for d in dates]

    def timeframes(self, exchange):
        return self.supported

    def archive(self, exchange):
        self.opened += 1
        if isinstance(self.zipped, Exception):
            raise self.zipped
        return self.zipped

    def download(self, exchange, args):
        self.calls.append(args)
        if self.fail:
            return False
        tfs = args[args.index("-t") + 1:args.index("--pairs")]
        pairs = args[args.index("--pairs") + 1:]
        backfill.datadir(exchange).mkdir(parents=True, exist_ok=True)
        for pair in pairs:
            for tf in tfs:
                f = backfill.series_file(exchange, pair, tf)
                freq = {"1h": "1h", "1d": "1D"}[tf]
                if "--prepend" in args:
                    candles(LISTED, NOW, freq).to_feather(f)
                elif not f.exists():
                    candles(NOW - dt.timedelta(days=1), NOW, freq).to_feather(f)
        return True


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "RESULTS", tmp_path)
    return tmp_path


def coverage(conn, exchange="binanceus"):
    rows = conn.execute("SELECT pair, timeframe, first_ts, last_ts, rows, gaps, missing_candles, oldest_reached "
                        "FROM data_coverage WHERE exchange = %s ORDER BY 1, 2", (exchange,)).fetchall()
    return {(r[0], r[1]): r[2:] for r in rows}


def test_prepend_until_the_first_candle_stops_moving(conn, lab):
    ex = FakeExchange()
    assert backfill.backfill_exchange(ex, conn, "binanceus", ["BTC/USD", "ETH/USD"], ["1h", "1d"])
    cov = coverage(conn)
    assert set(cov) == {("BTC/USD", "1h"), ("BTC/USD", "1d"), ("ETH/USD", "1h"), ("ETH/USD", "1d")}
    first, last, rows, gaps, missing, done = cov[("BTC/USD", "1h")]
    assert first == LISTED and last == NOW - dt.timedelta(hours=1) and rows == 48 and not done

    assert backfill.backfill_exchange(ex, conn, "binanceus", ["BTC/USD", "ETH/USD"], ["1h", "1d"])
    assert all(v[-1] for v in coverage(conn).values()), "a prepend that moved nothing marks the oldest candle"

    ex.calls.clear()
    assert backfill.backfill_exchange(ex, conn, "binanceus", ["BTC/USD", "ETH/USD"], ["1h", "1d"])
    assert ex.calls and not any("--prepend" in c for c in ex.calls), "a series at its oldest candle is not prepended"


def test_a_timeframe_floor_bounds_its_prepend(conn, lab, monkeypatch):
    monkeypatch.setattr(backfill, "PREPEND_FLOORS", {"1h": "20260101"})
    ex = FakeExchange()
    assert backfill.backfill_exchange(ex, conn, "binanceus", ["BTC/USD"], ["1h", "1d"])
    ranges = {c[c.index("-t") + 1]: c[c.index("--timerange") + 1] for c in ex.calls if "--prepend" in c}
    assert ranges == {"1h": "20260101-", "1d": f"{backfill.PREPEND_FROM}-"}


def test_unsupported_timeframe_is_skipped(conn, lab):
    ex = FakeExchange(supported=("1h",))
    assert backfill.backfill_exchange(ex, conn, "coinbase", ["BTC/USD"], ["1h", "1d"])
    assert set(coverage(conn, "coinbase")) == {("BTC/USD", "1h")}
    assert not any("1d" in c for c in ex.calls)


def test_forward_exchange_merges_the_newest_candles(conn, lab, monkeypatch):
    monkeypatch.setattr(backfill, "FORWARD_EXCHANGES", {"kraken"})
    ex = FakeExchange(supported=("1h",))
    assert backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    assert not ex.calls, "no download-data call for an exchange without history"
    first, last, rows, gaps, _, done = coverage(conn, "kraken")[("BTC/USD", "1h")]
    assert (rows, gaps, done) == (5, 0, False), "the forming candle is dropped"
    assert last == NOW - dt.timedelta(hours=1)

    ex.now = NOW + dt.timedelta(hours=2)  # overlapping window: merged, no duplicates
    backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    first2, last2, rows, gaps, *_ = coverage(conn, "kraken")[("BTC/USD", "1h")]
    assert (first2, last2, rows, gaps) == (first, NOW + dt.timedelta(hours=1), 7, 0)

    ex.now = NOW + dt.timedelta(hours=20)  # the job missed longer than the window: a gap, kept visible
    backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    _, _, rows, gaps, missing, _ = coverage(conn, "kraken")[("BTC/USD", "1h")]
    assert (rows, gaps, missing) == (12, 1, 13)


def zipped(members: dict[str, list[int]]) -> zipfile.ZipFile:
    """An archive of members, each a list of candle times, in Kraken's headerless layout."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, times in members.items():
            z.writestr(name, "".join(f"{t},1,3,0.5,2,10,4\n" for t in times))
    return zipfile.ZipFile(buf)


def seconds(start: dt.datetime, n: int, minutes: int) -> list[int]:
    return [int(start.timestamp()) + i * minutes * 60 for i in range(n)]


def test_archive_fills_the_past_once_and_collection_continues(conn, lab, monkeypatch):
    monkeypatch.setattr(backfill, "FORWARD_EXCHANGES", {"kraken"})
    past = seconds(NOW - dt.timedelta(hours=30), 24, 60)  # stops 6 hours before NOW: the newest 5 are collected
    ex = FakeExchange(supported=("1h",), zipped=zipped({"XBTUSD_60.csv": past}))
    assert backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    first, last, rows, gaps, _, done = coverage(conn, "kraken")[("BTC/USD", "1h")]
    assert first == NOW - dt.timedelta(hours=30) and last == NOW - dt.timedelta(hours=1)
    assert (rows, gaps, done) == (29, 1, True), "24 archived, 5 collected, the hole between them kept visible"

    assert backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    assert ex.opened == 1, "a series the archive has filled is not read again"


def test_archive_keeps_collection_running_when_it_is_down(conn, lab, monkeypatch):
    monkeypatch.setattr(backfill, "FORWARD_EXCHANGES", {"kraken"})
    ex = FakeExchange(supported=("1h",), zipped=OSError("no route"))
    assert not backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    _, _, rows, _, _, done = coverage(conn, "kraken")[("BTC/USD", "1h")]
    assert (rows, done) == (5, False), "forward candles age out, so they are still taken; the archive is retried"


def test_a_pair_the_archive_lacks_has_nothing_older(conn, lab, monkeypatch):
    monkeypatch.setattr(backfill, "FORWARD_EXCHANGES", {"kraken"})
    ex = FakeExchange(supported=("1h",), zipped=zipped({}))
    assert backfill.backfill_exchange(ex, conn, "kraken", ["BTC/USD"], ["1h"])
    assert coverage(conn, "kraken")[("BTC/USD", "1h")][-1] is True


def test_archive_names_and_columns():
    z = zipped({"XDGUSD_5.csv": seconds(LISTED, 3, 5)})
    assert archive.member("BTC/USD", "1h") == "XBTUSD_60.csv"
    df = archive.candles(z, "DOGE/USD", "5m")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["date"].iloc[0] == LISTED and len(df) == 3
    assert archive.candles(z, "ETH/USD", "5m") is None


def test_archive_floor_drops_what_is_older():
    z = zipped({"XBTUSD_1.csv": seconds(LISTED - dt.timedelta(minutes=2), 5, 1)})
    df = archive.candles(z, "BTC/USD", "1m", LISTED.strftime("%Y%m%d"))
    assert df["date"].iloc[0] == LISTED and len(df) == 3


def test_archive_builds_30m_from_15m():
    times = seconds(LISTED, 4, 15)
    del times[3]  # no trades in the last 15 minutes: the bucket is built from what there is
    times.append(int((LISTED + dt.timedelta(hours=2)).timestamp()))  # and an empty hour stays empty
    df = archive.candles(zipped({"XBTUSD_15.csv": times}), "BTC/USD", "30m")
    assert list(df["date"]) == [LISTED, LISTED + dt.timedelta(minutes=30), LISTED + dt.timedelta(hours=2)]
    assert list(df["volume"]) == [20, 10, 10]


def test_range_file_reads_a_zip_split_across_urls(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("XBTUSD_60.csv", "".join(f"{t},1,1,1,1,1,1\n" for t in range(5000)))
    data = buf.getvalue()
    third = len(data) // 3
    parts = {f"http://x/{i}": data[i * third:(i + 1) * third if i < 2 else None] for i in range(3)}

    class Response(io.BytesIO):
        def __init__(self, body, length):
            super().__init__(body)
            self.headers = {"Content-Length": str(length)}

    def urlopen(req, timeout):
        body = parts[req.full_url]
        if req.get_method() == "HEAD":
            return Response(b"", len(body))
        a, b = req.get_header("Range").removeprefix("bytes=").split("-")
        return Response(body[int(a):int(b) + 1], int(b) + 1 - int(a))

    monkeypatch.setattr(archive.urllib.request, "urlopen", urlopen)
    z = zipfile.ZipFile(archive.RangeFile(list(parts)))
    assert z.read("XBTUSD_60.csv").count(b"\n") == 5000, "zipfile checks the CRC on read"


def test_failed_download_still_records_what_is_on_disk(conn, lab):
    assert not backfill.backfill_exchange(FakeExchange(fail=True), conn, "binanceus", ["BTC/USD"], ["1h"])
    first, last, rows, *_ = coverage(conn)[("BTC/USD", "1h")]
    assert (first, last, rows) == (None, None, 0)


def test_dropped_pair_loses_its_row(conn, lab):
    ex = FakeExchange()
    backfill.backfill_exchange(ex, conn, "binanceus", ["BTC/USD", "ETH/USD"], ["1h"])
    backfill.backfill_exchange(ex, conn, "binanceus", ["BTC/USD"], ["1h"])
    assert set(coverage(conn)) == {("BTC/USD", "1h")}


def test_scan_counts_gaps(tmp_path):
    f = tmp_path / "x.feather"
    candles(LISTED, NOW, "1h", drop=3).to_feather(f)
    r = backfill.scan(f, "1h")
    assert (r["rows"], r["gaps"], r["missing_candles"]) == (45, 1, 3)
    assert backfill.scan(tmp_path / "missing.feather", "1h")["rows"] == 0
