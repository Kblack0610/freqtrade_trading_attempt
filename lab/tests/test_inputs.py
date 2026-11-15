import datetime as dt

import pytest
import requests

from lab import inputs, nws, taxonomy
from lab.markets import families
from lab.markets.api import Api
from lab.tests.markets_fakes import Resp, Session

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 15, 12, tzinfo=UTC)
POINT = {"properties": {"gridId": "OKX", "gridX": 33, "gridY": 37, "timeZone": "America/New_York"}}
FORECAST = {"properties": {"updateTime": "2026-11-15T10:12:00+00:00", "periods": [
    {"startTime": "2026-11-15T06:00:00-05:00", "isDaytime": True, "temperature": 58, "temperatureUnit": "F"},
    {"startTime": "2026-11-15T18:00:00-05:00", "isDaytime": False, "temperature": 44, "temperatureUnit": "F"},
    {"startTime": "2026-11-16T06:00:00-05:00", "isDaytime": True, "temperature": 15, "temperatureUnit": "C"}]}}


def test_every_input_a_family_or_strategy_reads_is_in_the_registry():
    reg = inputs.load()
    for f in (families.Longshot, families.TTTotals, families.Weather):
        assert set(f.INPUTS) <= reg.keys(), f.name
    taxonomy.load()  # every strategy's inputs and every signal_source


def test_an_input_without_its_fields_or_a_reader_without_a_limit_is_refused(tmp_path):
    text = inputs.CONFIG.read_text()
    for broken in (text.replace('origin = "the exchange\'s OHLCV, via Freqtrade"\n', "", 1),
                   text.replace("fresh_min = 180\n", "", 1)):
        path = tmp_path / "inputs.toml"
        path.write_text(broken)
        with pytest.raises(ValueError):
            inputs.load(path)
    assert inputs.source(inputs.load(), "yes_depth", "kalshi") == "kalshi recorder"


def test_an_input_is_stale_past_its_limit_or_never_read():
    reg = inputs.load()
    assert inputs.freshness({}, NOW, reg) == [{"input": "nws_high", "title": reg["nws_high"]["title"], "newest": None,
                                               "fresh_min": 180, "stale": True}]
    assert not inputs.freshness({"nws_high": NOW - dt.timedelta(minutes=180)}, NOW, reg)[0]["stale"]
    assert inputs.freshness({"nws_high": NOW - dt.timedelta(minutes=181)}, NOW, reg)[0]["stale"]


def test_the_forecast_high_is_each_daytime_period_in_whole_degrees_f():
    assert nws.highs(FORECAST) == {"2026-11-15": 58, "2026-11-16": 59}


class NotFound(Resp):
    def raise_for_status(self):
        raise requests.HTTPError("404")


def nws_api(*answers):
    cfg = {**inputs.load()["nws_high"]["api"], "budget_rps": None, "page_gap_s": 0}
    return Api("nws", cfg, Session(*answers), sleep=lambda s: None)


def test_the_reader_names_itself_to_the_api():
    s = requests.Session()
    nws.client(inputs.load()["nws_high"]["api"], s)
    assert s.headers["User-Agent"].startswith("trader-lab") and "@" not in s.headers["User-Agent"]


def test_the_reader_writes_one_reading_a_station_and_reports_a_station_that_fails(conn):
    reg = inputs.load()
    reg = {**reg, "nws_high": {**reg["nws_high"], "stations": {k: reg["nws_high"]["stations"][k] for k in ("KMDW", "KNYC")}}}
    api = nws_api((200, POINT), (200, {"properties": {}}), (200, POINT), (200, FORECAST))
    assert nws.run(conn, api, reg, NOW) == (1, ["KMDW"])  # KMDW's forecast has no periods
    assert api.http.calls[0][0] == "https://api.weather.gov/points/41.7842,-87.7553"
    assert api.http.calls[3][0] == "https://api.weather.gov/gridpoints/OKX/33,37/forecast"
    [(key, as_of, value)] = inputs.readings(conn, "nws_high", NOW, NOW + dt.timedelta(seconds=1))
    assert (key, as_of, value) == ("KNYC", NOW, {"highs": {"2026-11-15": 58, "2026-11-16": 59},
                                                "updated": "2026-11-15T10:12:00+00:00", "grid": "OKX/33,37",
                                                "tz": "America/New_York"})
    inputs.write(conn, "nws_high", "KNYC", NOW, {"highs": {}})  # the same read again keeps the first
    assert inputs.readings(conn, "nws_high", NOW, NOW + dt.timedelta(seconds=1))[0][2]["highs"]["2026-11-15"] == 58
    assert inputs.days(conn, "nws_high") == {NOW.date()}
    api = nws_api((200, POINT), NotFound(404))
    assert nws.run(conn, api, {**reg, "nws_high": {**reg["nws_high"], "stations": {"KNYC": reg["nws_high"]["stations"]["KNYC"]}}},
                   NOW + dt.timedelta(hours=1)) == (0, ["KNYC"])


def test_input_metrics_give_each_read_input_its_age_and_limit():
    from web.exporter import input_metrics

    got = {m.name: [(s.labels, s.value) for s in m.samples] for m in input_metrics(NOW, {"nws_high": NOW - dt.timedelta(hours=1)})}
    assert got == {"trader_input_age_seconds": [({"input": "nws_high"}, 3600.0)],
                   "trader_input_fresh_seconds": [({"input": "nws_high"}, 10800.0)]}
    assert [m.samples for m in input_metrics(NOW, {})][0] == []  # never read: no age, absent() fires
