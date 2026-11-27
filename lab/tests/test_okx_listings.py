import datetime as dt
import io
import json
import zipfile

import pandas as pd

from lab.crypto import archive, okx_listings

UTC = dt.timezone.utc
CDN = archive.OKX_FILES.base
NOW = pd.Timestamp("2026-11-27", tz="UTC")


def ms(*a):
    return int(dt.datetime(*a, tzinfo=UTC).timestamp() * 1000)


def okx_zip(inst: str, month: str, times_ms: list[int]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{inst}-candlesticks-{month}.csv",
                   "instrument_name,open,high,low,close,vol,vol_ccy,vol_quote,open_time,confirm\n"
                   + "".join(f"{inst},1,3,0.5,2,10,20,20,{t},1\n" for t in times_ms))
    return buf.getvalue()


SRM_FIRST, SRM_LAST = ms(2020, 8, 14, 1), ms(2023, 10, 17, 3)
FILES = {
    "SRM-USDT": {f"{CDN}/202008/SRM-USDT-2020-08.zip": okx_zip("SRM-USDT", "2020-08", [SRM_FIRST, SRM_FIRST + 60_000]),
                 f"{CDN}/202310/SRM-USDT-2023-10.zip": okx_zip("SRM-USDT", "2023-10", [SRM_LAST - 60_000, SRM_LAST])},
    "BTC-USDT": {f"{CDN}/202001/BTC-USDT-2020-01.zip": okx_zip("BTC-USDT", "2020-01", [ms(2020, 1, 1, 1)]),
                 f"{CDN}/202611/BTC-USDT-2026-11.zip": okx_zip("BTC-USDT", "2026-11", [ms(2026, 11, 26, 23)])},
}
TARDIS = {"availableSymbols": [
    {"id": "SRM-USDT", "type": "spot", "availableSince": "2020-08-14T00:00:00.000Z", "availableTo": "2023-10-17T00:00:00.000Z"},
    {"id": "BTC-USDT", "type": "spot", "availableSince": "2019-03-30T00:00:00.000Z"},
    {"id": "OLD-USDT", "type": "spot", "availableSince": "2018-01-01T00:00:00.000Z", "availableTo": "2019-06-01T00:00:00.000Z"},
    {"id": "FTT-USDT", "type": "spot", "availableSince": "2021-01-01T00:00:00.000Z", "availableTo": "2022-11-10T00:00:00.000Z"},
    {"id": "BTC-EUR", "type": "spot", "availableSince": "2021-01-01T00:00:00.000Z"},
    {"id": "BTC-USDT-SWAP", "type": "swap", "availableSince": "2021-01-01T00:00:00.000Z"}]}


class FakeTardis:
    def get(self, path, params=None, label=None):
        assert path == "/exchanges/okex"
        return TARDIS


def fake_get(api, url, params=None):
    if url in {u for f in FILES.values() for u in f}:
        return next(f[url] for f in FILES.values() if url in f)
    assert (api, url) == (archive.OKX, archive.OKX_LIST)
    groups = [{"url": u} for u in FILES.get(params["instIdList"], {})]
    return json.dumps({"code": "0", "msg": "", "data": [{"details": [{"groupDetails": groups}]}]}).encode()


def setup(monkeypatch):
    monkeypatch.setattr(okx_listings, "TARDIS", FakeTardis())
    monkeypatch.setattr(archive, "_get", fake_get)


def test_ids_keep_spot_usdt_only(monkeypatch):
    setup(monkeypatch)
    assert [s["id"] for s in okx_listings.ids()] == ["SRM-USDT", "BTC-USDT", "OLD-USDT", "FTT-USDT"]


def test_a_pair_that_ended_before_the_archive_has_no_span():
    since, until = pd.Timestamp("2018-01-01", tz="UTC"), pd.Timestamp("2019-06-01", tz="UTC")
    assert okx_listings.span(since, until, NOW) is None


def test_srm_is_listed_through_2023_10_and_its_candles_load(conn, monkeypatch):
    setup(monkeypatch)
    found, missing, failed = okx_listings.refresh(conn, NOW)
    assert set(found) == {"SRM/USDT", "BTC/USDT"} and failed == []
    assert missing == ["OLD/USDT", "FTT/USDT"], "ids with no archive file are counted, not dropped"
    first, last = conn.execute("SELECT first_ts, last_ts FROM pair_windows WHERE exchange = 'okx' AND pair = 'SRM/USDT'").fetchone()
    assert (first, last) == (dt.datetime(2020, 8, 14, 1, tzinfo=UTC), dt.datetime(2023, 10, 17, 3, tzinfo=UTC))
    months = [r[0] for r in conn.execute("SELECT month FROM pair_listings WHERE exchange = 'okx' AND pair = 'SRM/USDT' ORDER BY 1")]
    assert months[0] == dt.date(2020, 8, 1) and months[-1] == dt.date(2023, 10, 1) and dt.date(2023, 11, 1) not in months
    status = dict(conn.execute("SELECT pair, status FROM pair_windows WHERE exchange = 'okx'").fetchall())
    assert status == {"SRM/USDT": "ENDED", "BTC/USDT": "LIVE"}
    df = archive.okx_candles("SRM/USDT", "20231001")
    assert list(df["date"].dt.strftime("%Y-%m")) == ["2023-10", "2023-10"] and len(df) == 2


def test_an_unreadable_id_keeps_its_old_rows(conn, monkeypatch):
    setup(monkeypatch)
    okx_listings.refresh(conn, NOW)
    real = archive.okx_window
    monkeypatch.setattr(archive, "okx_window", lambda p, a, b: (_ for _ in ()).throw(OSError("down")) if p == "SRM/USDT" else real(p, a, b))
    found, missing, failed = okx_listings.refresh(conn, NOW)
    assert failed == ["SRM/USDT"] and set(found) == {"BTC/USDT"}
    assert conn.execute("SELECT count(*) FROM pair_listings WHERE exchange = 'okx' AND pair = 'SRM/USDT'").fetchone()[0] > 0
