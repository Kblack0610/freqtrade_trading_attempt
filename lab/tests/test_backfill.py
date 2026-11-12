import datetime as dt
import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from lab import archive, backfill

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

    def __init__(self, supported=("1h", "1d"), fail=False, zipped=None, bulked=None):
        self.supported = list(supported)
        self.zipped = zipped
        self.bulked = bulked  # what bulk() returns, or raises
        self.bulks: list[tuple[str, str]] = []
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

    def bulk(self, exchange, pair, timeframe):
        self.bulks.append((pair, timeframe))
        if isinstance(self.bulked, Exception):
            raise self.bulked
        return self.bulked

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
                    old = pd.read_feather(f) if f.exists() else None  # what a bulk import left: kept
                    pd.concat([candles(LISTED, NOW if old is None else old["date"].min(), freq), old]).to_feather(f)
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
    assert backfill.backfill_exchange(ex, conn, "coinbase", ["BTC/USD"], ["1h", "30m"])
    assert set(coverage(conn, "coinbase")) == {("BTC/USD", "1h")}, "nothing served is short enough to build 30m from"
    assert not any("30m" in c for c in ex.calls)


def test_unserved_timeframe_is_built_from_a_shorter_one(conn, lab):
    ex = FakeExchange(supported=("1h",))
    assert backfill.backfill_exchange(ex, conn, "coinbase", ["BTC/USD"], ["1h", "4h"])
    assert not any("4h" in c for c in ex.calls)
    first, last, rows, gaps, _, done = coverage(conn, "coinbase")[("BTC/USD", "4h")]
    assert (first, last, rows, gaps, done) == (LISTED, NOW - dt.timedelta(hours=4), 12, 0, False)
    df = pd.read_feather(backfill.series_file("coinbase", "BTC/USD", "4h"))
    assert set(df["volume"]) == {4.0}, "four 1h candles in each"

    backfill.backfill_exchange(ex, conn, "coinbase", ["BTC/USD"], ["1h", "4h"])
    assert coverage(conn, "coinbase")[("BTC/USD", "4h")][-1] is True, "done when its source is"


def test_an_unfinished_last_bucket_is_left_out(lab):
    backfill.datadir("coinbase").mkdir(parents=True)
    candles(LISTED, LISTED + dt.timedelta(hours=7), "1h").to_feather(backfill.series_file("coinbase", "BTC/USD", "1h"))
    backfill.derive("coinbase", "BTC/USD", "1h", "4h")
    df = pd.read_feather(backfill.series_file("coinbase", "BTC/USD", "4h"))
    assert list(df["date"]) == [LISTED], "00:00-04:00 is whole; 04:00 has only three of its four hours"


def test_source_of():
    assert backfill.source_of("4h", ["1m", "5m", "1h", "1d"]) == "1h"
    assert backfill.source_of("30m", ["1h"]) is None


def test_bulk_history_fills_a_new_series_before_the_prepend(conn, lab):
    ex = FakeExchange(bulked=candles(LISTED, NOW - dt.timedelta(hours=12), "1h"))
    assert backfill.backfill_exchange(ex, conn, "okx", ["BTC/USDT"], ["1h"])
    first, *_ = coverage(conn, "okx")[("BTC/USDT", "1h")]
    assert first == LISTED and ex.bulks == [("BTC/USDT", "1h")]

    backfill.backfill_exchange(ex, conn, "okx", ["BTC/USDT"], ["1h"])
    assert ex.bulks == [("BTC/USDT", "1h")], "a series that has candles pages the rest from the API"
    assert coverage(conn, "okx")[("BTC/USDT", "1h")][-1] is True


def test_bulk_failure_falls_back_to_the_prepend(conn, lab):
    ex = FakeExchange(bulked=OSError("no route"))
    assert backfill.backfill_exchange(ex, conn, "okx", ["BTC/USDT"], ["1h"])
    assert coverage(conn, "okx")[("BTC/USDT", "1h")][0] == LISTED


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


def okx_zip(month: str, times_ms: list[int], confirm: int = 1) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BTC-USDT-candlesticks-{month}.csv",
                   "instrument_name,open,high,low,close,vol,vol_ccy,vol_quote,open_time,confirm\n"
                   + "".join(f"BTC-USDT,1,3,0.5,2,10,20,20,{t},{confirm}\n" for t in times_ms))
    return buf.getvalue()


def test_okx_candles_from_monthly_files(monkeypatch):
    start = LISTED - dt.timedelta(minutes=2)
    ms = [int(start.timestamp() * 1000) + i * 60_000 for i in range(5)]
    cdn = archive.OKX_FILES.base
    files = {f"{cdn}/1.zip": okx_zip("2026-01", ms), f"{cdn}/2.zip": okx_zip("2026-02", ms[-1:] + [ms[-1] + 60_000])}
    windows = []

    def get(api, url, params=None):
        if url in files:
            assert api is archive.OKX_FILES
            return files[url]
        assert (api, url) == (archive.OKX, archive.OKX_LIST)
        windows.append((params["begin"], params["end"]))
        groups = [{"url": u} for u in files] if len(windows) == 1 else []
        return json.dumps({"code": "0", "msg": "", "data": [{"details": [{"groupDetails": groups}]}]}).encode()

    monkeypatch.setattr(archive, "_get", get)
    df = archive.okx_candles("BTC/USDT", LISTED.strftime("%Y%m%d"))
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["date"].iloc[0] == LISTED and len(df) == 4, "older than the floor dropped, the overlap kept once"
    assert df["volume"].iloc[0] == 10, "vol is the base-currency volume freqtrade stores"
    assert all(b - a <= 300 * 86400_000 for a, b in windows) and len(windows) > 1, "the listing pages under 10 months"


def test_okx_listing_error_is_raised(monkeypatch):
    monkeypatch.setattr(archive, "_get", lambda api, url, params=None: b'{"code":"50077","msg":"too long","data":[]}')
    with pytest.raises(RuntimeError, match="50077"):
        archive.okx_candles("BTC/USDT", "20260101")


@pytest.mark.skipif(not (backfill.REPO / "user_data" / "strategies").is_dir(), reason="runs in the image")
def test_the_image_has_every_userdir_folder(monkeypatch):
    """freqtrade's check-then-create races across parallel processes, so none may be left for it to create."""
    from freqtrade.configuration import directory_operations

    def mkdir(self, *a, **k):
        raise AssertionError(f"{self} is missing from the image (Dockerfile create-userdir)")
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(directory_operations, "chown_user_directory", lambda d: None)  # sudo chown in docker
    directory_operations.create_userdata_dir(str(backfill.REPO / "user_data"))


def test_range_file_reads_a_zip_split_across_urls():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("XBTUSD_60.csv", "".join(f"{t},1,1,1,1,1,1\n" for t in range(5000)))
    data = buf.getvalue()
    third = len(data) // 3
    parts = {f"http://x/{i}": data[i * third:(i + 1) * third if i < 2 else None] for i in range(3)}

    class Response:
        def __init__(self, body, length):
            self.content, self.status_code, self.headers = body, 206, {"Content-Length": str(length)}

        def raise_for_status(self):
            pass

    class Session:
        def request(self, method, url, params, json, headers, timeout):
            assert headers["Accept-Encoding"] == "identity", "a range must be the file's own bytes"
            body = parts[url]
            if method == "HEAD":
                return Response(b"", len(body))
            a, b = headers["Range"].removeprefix("bytes=").split("-")
            return Response(body[int(a):int(b) + 1], int(b) + 1 - int(a))

    api = archive.Api("t", {"base_url": "http://x", "page_gap_s": 0}, Session(), sleep=lambda _: None)
    z = zipfile.ZipFile(archive.RangeFile(list(parts), api))
    assert z.read("XBTUSD_60.csv").count(b"\n") == 5000, "zipfile checks the CRC on read"
    with pytest.raises(ValueError, match="refusing"):
        archive.RangeFile(["http://elsewhere/0"], api)


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


def test_a_halted_market_is_a_gap_and_a_quiet_one_is_kept(conn, lab):
    """binanceus 1d pads its USD suspension with flat zero-volume days; a backtest would trade the frozen price."""
    days = candles(LISTED - dt.timedelta(days=60), NOW, "1D")
    flat = days["date"].between(LISTED - dt.timedelta(days=50), LISTED - dt.timedelta(days=41))  # 10 days
    quiet = days["date"].between(LISTED - dt.timedelta(days=20), LISTED - dt.timedelta(days=19))  # 2 days
    days.loc[flat | quiet, "volume"] = 0.0
    backfill.datadir("binanceus").mkdir(parents=True)
    days.to_feather(backfill.series_file("binanceus", "BTC/USD", "1d"))

    assert backfill.backfill_exchange(FakeExchange(), conn, "binanceus", ["BTC/USD"], ["1d"])
    _, _, rows, gaps, missing, _ = coverage(conn)[("BTC/USD", "1d")]
    assert (rows, gaps, missing) == (len(days) - 10, 1, 10)
    left = pd.read_feather(backfill.series_file("binanceus", "BTC/USD", "1d"))
    assert (left["volume"] == 0).sum() == 2, "a quiet run under HALT_DAYS stays"
