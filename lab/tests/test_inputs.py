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
    for f in (families.Longshot, families.TTTotals, families.Weather, families.Observed):
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
    never = {r["input"]: r for r in inputs.freshness({}, NOW, reg)}
    assert never.keys() == {"nws_cli", "nws_high", "nws_obs"} and all(r["stale"] and r["newest"] is None for r in never.values())
    assert never["nws_high"] == {"input": "nws_high", "title": reg["nws_high"]["title"], "newest": None,
                                 "fresh_min": 180, "stale": True}
    stale = lambda age: {r["input"]: r for r in inputs.freshness({"nws_high": NOW - dt.timedelta(minutes=age)}, NOW, reg)}["nws_high"]["stale"]  # noqa: E731
    assert not stale(180) and stale(181)


def test_a_healthy_climate_report_reader_is_never_stale():
    """A report's as_of is its day's end and it is read FINAL_H later, at the next hourly run (lab.nws.run_cli), so the
    newest one gets older than the longest gap between two stations' day ends; fresh_min must stay above that peak."""
    reg = inputs.load()
    stations = reg[nws.NAME]["stations"].values()
    ends = sorted({nws.day_start(dt.date(2026, 11, 20) + dt.timedelta(days=k), st) for k in range(3) for st in stations})
    peak = max(b - a for a, b in zip(ends, ends[1:])) + dt.timedelta(hours=nws.FINAL_H + 1)
    assert peak < dt.timedelta(minutes=reg[nws.CLI]["fresh_min"])  # 34 h on the 2026-11 registry


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
                   "trader_input_fresh_seconds": [({"input": "nws_cli"}, 126000.0), ({"input": "nws_high"}, 10800.0),
                                                  ({"input": "nws_obs"}, 10800.0)]}
    assert [m.samples for m in input_metrics(NOW, {})][0] == []  # never read: no age, absent() fires


# observations, the climate report and the forecast's error

KNYC = inputs.load()["nws_high"]["stations"]["KNYC"]
NOON = dt.datetime(2026, 11, 15, 17, tzinfo=UTC)  # noon in New York, local standard time


def feature(ts, c, mm=None):
    return {"properties": {"timestamp": ts, "temperature": {"value": c}, "precipitationLastHour": {"value": mm}}}


class Text(Resp):
    def __init__(self, text):
        super().__init__(200)
        self.text = text


def test_a_station_day_is_its_local_standard_day_whatever_the_clock_says():
    assert nws.local_day(dt.datetime(2026, 7, 15, 4, 30, tzinfo=UTC), KNYC) == dt.date(2026, 7, 14)  # 11:30 pm EST
    assert nws.day_start(dt.date(2026, 7, 15), KNYC) == dt.datetime(2026, 7, 15, 5, tzinfo=UTC)  # no daylight saving


def test_a_day_so_far_keeps_the_highest_and_lowest_whole_degree_and_whether_measured_rain_fell():
    body = {"features": [feature("2026-11-15T17:51:00+00:00", 12.8), feature("2026-11-15T05:51:00+00:00", 4.4, 0.0),
                         feature("2026-11-15T11:51:00+00:00", 7.0, 0.3), feature("2026-11-15T13:51:00+00:00", None)]}
    got = nws.summarize(nws.station_obs(body), dt.date(2026, 11, 15))
    assert got == {"day": "2026-11-15", "high": 55, "low": 40, "rained": True, "n": 4, "last": "2026-11-15T17:51:00+00:00"}
    dry = nws.summarize(nws.station_obs({"features": [feature("2026-11-15T05:51:00+00:00", 4.4, 0.1)]}), dt.date(2026, 11, 15))
    assert dry["rained"] is False  # 0.1 mm is a trace, not 0.01 in
    assert nws.summarize([], dt.date(2026, 11, 15)) is None


def test_the_observation_reader_asks_for_the_stations_local_day_and_a_quiet_station_is_not_a_failure(conn):
    reg = inputs.load()
    reg = {**reg, "nws_high": {**reg["nws_high"], "stations": {"KNYC": KNYC}}}
    api = nws_api((200, {"features": [feature("2026-11-15T16:51:00+00:00", 10.0, 0.3)]}))
    assert nws.run_obs(conn, api, reg, NOON) == (1, [])
    url, params = api.http.calls[0]
    assert url == "https://api.weather.gov/stations/KNYC/observations"
    assert params == {"start": "2026-11-15T05:00:00Z", "limit": 500}
    [(key, as_of, value)] = inputs.readings(conn, "nws_obs", NOON, NOON + dt.timedelta(seconds=1))
    assert (key, as_of, value["high"], value["rained"]) == ("KNYC", NOON, 50, True)
    assert nws.run_obs(conn, nws_api((200, {"features": []})), reg, NOON + dt.timedelta(hours=1)) == (0, [])
    assert nws.run_obs(conn, nws_api(NotFound(404)), reg, NOON + dt.timedelta(hours=2)) == (0, ["KNYC"])


def test_the_asos_archive_becomes_one_running_reading_an_observation_and_only_rain_of_a_hundredth_counts():
    csv = ("station,valid,tmpf,p01i\nNYC,2026-11-15 04:51,50.00,0.0000\nNYC,2026-11-15 06:51,44.00,0.0001\n"
           "NYC,2026-11-15 17:51,56.00,\nNYC,2026-11-15 18:51,,0.0200\n")
    api = nws_api(Text(csv))
    obs = nws.asos(api, "KNYC", KNYC, dt.date(2026, 11, 15), dt.date(2026, 11, 16))
    assert api.http.calls[0][1]["station"] == "NYC" and api.http.calls[0][1]["data"] == ["tmpf", "p01i"]
    got = list(nws.running(obs, KNYC, dt.timedelta(minutes=10)))
    assert [(a.isoformat(), v["day"], v["high"], v["low"], v["rained"]) for a, v in got] == [
        ("2026-11-15T05:01:00+00:00", "2026-11-14", 50, 50, False),  # 11:51 pm on the 14th, local standard
        ("2026-11-15T07:01:00+00:00", "2026-11-15", 44, 44, False),  # a new day starts over; a trace is not rain
        ("2026-11-15T18:01:00+00:00", "2026-11-15", 56, 44, False),
        ("2026-11-15T19:01:00+00:00", "2026-11-15", 56, 44, True)]


def test_a_finished_days_climate_report_is_stored_once_and_gives_the_forecast_its_error(conn):
    reg = inputs.load()
    reg = {**reg, "nws_high": {**reg["nws_high"], "stations": {"KNYC": KNYC}}}
    body = {"results": [{"valid": "2026-11-13", "high": 60, "low": 41, "precip": "T"},
                        {"valid": "2026-11-14", "high": 57, "low": 40, "precip": 0.12},
                        {"valid": "2026-11-15", "high": "M", "low": "M", "precip": "M"}]}
    now = dt.datetime(2026, 11, 15, 18, tzinfo=UTC)
    assert nws.run_cli(conn, nws_api((200, body)), reg, now) == (2, [])
    assert nws.run_cli(conn, nws_api((200, body)), reg, now) == (0, [])  # the same report is not stored again
    assert nws.run_cli(conn, nws_api((200, {"results": [{"valid": "2026-11-15", "high": 50, "low": 40, "precip": 0}]})),
                       reg, now) == (0, [])  # the 15th ended less than FINAL_H ago
    stored = {v["day"]: v["precip"] for _, _, v in inputs.readings(conn, "nws_cli", dt.datetime(2026, 11, 1, tzinfo=UTC), now)}
    assert stored == {"2026-11-13": nws.TRACE, "2026-11-14": 0.12}
    for h, high in ((0, 59), (12, 54)):  # a morning forecast of the 14th and a later one that must not count
        inputs.write(conn, "nws_high", "KNYC", dt.datetime(2026, 11, 14, 5, tzinfo=UTC) + dt.timedelta(hours=h),
                     {"highs": {"2026-11-14": high}})
    errors = nws.forecast_errors(conn, reg)
    assert errors == {"KNYC": [57 - 59]}  # the 13th has a report but no forecast
    assert nws.sd_report(errors) == ["KNYC: 1 days, bias -2.00, sd 0.00", "all: 1 days, bias -2.00, sd 0.00"]
