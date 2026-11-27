import datetime as dt
import io
import zipfile

import pandas as pd

from lab.crypto import kucoin_listings as kl

UTC = dt.timezone.utc
NOW = pd.Timestamp("2026-11-27", tz="UTC")
NS = "http://s3.amazonaws.com/doc/2006-03-01/"
PAGE = 2  # a tiny page so marker paging is exercised


def ts(*a):
    return int(dt.datetime(*a, tzinfo=UTC).timestamp())


def day_zip(rows: list[int]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "time,open,close,high,low,volume,turnover\n"
                   + "".join(f"{t},1,2,3,0.5,10,20\n" for t in rows))
    return buf.getvalue()


# close comes before high and low in the file: 2 is the close, 3 the high, 0.5 the low
BUCKET = {
    "2CRZUSDT": {"2022-12-31": [ts(2022, 12, 31, 5)], "2023-08-11": [ts(2023, 8, 11, 1), ts(2023, 8, 11, 0)]},
    "BTCUSDT": {"2022-12-31": [ts(2022, 12, 31, 0)], "2026-11-26": [ts(2026, 11, 26, 23)]},
    "EMPTYUSDT": {"2023-03-01": [], "2023-03-02": [ts(2023, 3, 2, 1)], "2023-03-03": []},
    "NONEUSDT": {"2023-03-01": []},
}
OTHER = ["ETHBTC", "ZZZ"]  # not quoted in USDT


class Resp:
    def __init__(self, content=b"", status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        assert self.status_code == 200


class FakeBucket:
    def __init__(self):
        self.fail = set()
        self.calls = []

    def request(self, method, path, params=None, **kw):
        self.calls.append((path, params))
        if params is not None and "prefix" in params:
            return self.listing(params["prefix"], params["marker"])
        sym, _, name = path[len("/" + kl.ROOT):].partition("/1h/")
        if sym in self.fail:
            return Resp(status=500)
        return Resp(day_zip(BUCKET[sym][name[len(sym) + 4:-4]]))

    def listing(self, prefix, marker):
        if prefix == kl.ROOT:
            items = [(f"{kl.ROOT}{s}/", "p") for s in sorted([*BUCKET, *OTHER])]
        else:
            sym = prefix[len(kl.ROOT):].split("/")[0]
            items = [(f"{prefix}{sym}-1h-{d}.zip{suffix}", "k") for d in BUCKET[sym] for suffix in ("", ".CHECKSUM")]
        items = [i for i in items if i[0] > marker][:PAGE]
        more = items and any(k > items[-1][0] for k in self.all_after(prefix, items[-1][0]))
        body = "".join(f"<Contents><Key>{k}</Key></Contents>" if t == "k" else f"<CommonPrefixes><Prefix>{k}</Prefix></CommonPrefixes>" for k, t in items)
        nxt = f"<NextMarker>{items[-1][0]}</NextMarker>" if more and prefix != kl.ROOT else ""  # prefixes page with no NextMarker on some S3s
        return Resp(f'<ListBucketResult xmlns="{NS}"><IsTruncated>{"true" if more else "false"}</IsTruncated>{nxt}{body}</ListBucketResult>'.encode())

    def all_after(self, prefix, key):
        if prefix == kl.ROOT:
            return [f"{kl.ROOT}{s}/" for s in [*BUCKET, *OTHER] if f"{kl.ROOT}{s}/" > key]
        sym = prefix[len(kl.ROOT):].split("/")[0]
        return [k for d in BUCKET[sym] for k in (f"{prefix}{sym}-1h-{d}.zip", f"{prefix}{sym}-1h-{d}.zip.CHECKSUM") if k > key]


def setup(monkeypatch):
    fake = FakeBucket()
    monkeypatch.setattr(kl, "BUCKET", fake)
    return fake


def test_symbols_page_by_marker_and_keep_usdt_only(monkeypatch):
    setup(monkeypatch)
    assert kl.symbols() == ["2CRZUSDT", "BTCUSDT", "EMPTYUSDT", "NONEUSDT"]


def test_days_skip_the_checksum_files(monkeypatch):
    setup(monkeypatch)
    assert kl.days("2CRZUSDT") == [dt.date(2022, 12, 31), dt.date(2023, 8, 11)]


def test_candles_read_columns_by_name_in_time_order(monkeypatch):
    setup(monkeypatch)
    df = kl.candles("2CRZUSDT", start=dt.date(2023, 8, 1))
    assert list(df["date"].dt.hour) == [0, 1]
    assert df.iloc[0][["open", "high", "low", "close"]].tolist() == [1, 3, 0.5, 2]


def test_a_missing_column_is_an_error(monkeypatch):
    fake = setup(monkeypatch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "time,open,high,low\n1,1,1,1\n")
    fake.request = lambda m, path, params=None, **kw: Resp(buf.getvalue())
    try:
        kl.day_file("2CRZUSDT", "1h", dt.date(2023, 8, 11))
    except ValueError as e:
        assert "close" in str(e)
    else:
        raise AssertionError("a file without a close column was read")


def test_2crz_is_listed_in_2023_07_and_not_in_2023_09(conn, monkeypatch):
    setup(monkeypatch)
    found, failed = kl.refresh(conn, NOW)
    assert failed == [] and set(found) == {"2CRZ/USDT", "BTC/USDT", "EMPTY/USDT"}, "a symbol with no candle is not listed"
    first, last = conn.execute("SELECT first_ts, last_ts FROM pair_windows WHERE exchange = 'kucoin' AND pair = '2CRZ/USDT'").fetchone()
    assert (first, last) == (dt.datetime(2022, 12, 31, 5, tzinfo=UTC), dt.datetime(2023, 8, 11, 1, tzinfo=UTC))
    listed = lambda m: {r[0] for r in conn.execute("SELECT pair FROM pair_listings WHERE exchange = 'kucoin' AND month = %s", (m,))}
    assert "2CRZ/USDT" in listed(dt.date(2023, 7, 1)) and "2CRZ/USDT" not in listed(dt.date(2023, 9, 1))
    assert dt.date(2022, 12, 1) not in {r[0] for r in conn.execute("SELECT month FROM pair_listings WHERE exchange = 'kucoin'")}, "months start at 2023-01"
    assert dict(conn.execute("SELECT pair, status FROM pair_windows WHERE exchange = 'kucoin'").fetchall()) == {
        "2CRZ/USDT": "ENDED", "BTC/USDT": "LIVE", "EMPTY/USDT": "ENDED"}
    assert conn.execute("SELECT first_ts FROM pair_windows WHERE pair = 'EMPTY/USDT'").fetchone()[0] == dt.datetime(2023, 3, 2, 1, tzinfo=UTC), "an empty edge file is stepped over"


def test_an_unreadable_symbol_keeps_its_old_rows(conn, monkeypatch):
    fake = setup(monkeypatch)
    kl.refresh(conn, NOW)
    fake.fail.add("2CRZUSDT")
    found, failed = kl.refresh(conn, NOW)
    assert failed == ["2CRZ/USDT"] and "2CRZ/USDT" not in found
    assert conn.execute("SELECT count(*) FROM pair_listings WHERE exchange = 'kucoin' AND pair = '2CRZ/USDT'").fetchone()[0] > 0
