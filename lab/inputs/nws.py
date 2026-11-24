#!/usr/bin/env python3
"""The NWS readers behind the weather families: the forecast high, what a station has measured so far today, and the
daily climate report (CLI) every Kalshi weather market settles on. One input_readings row each, per station of
lab/inputs/inputs.toml [inputs.nws_high.stations].

  python -m lab.inputs.nws                                 # the hourly CronJob (home-config trader-inputs-nws): all three
  python -m lab.inputs.nws backfill [--station K..] --since 2026-10-01 [--until 2026-11-01]
  python -m lab.inputs.nws sd                              # how far the forecast high ran from the CLI high, per station

nws_high: api.weather.gov, free and keyless, through lab/core/api.py's paced client. For each station:
/points/<lat>,<lon> names its forecast grid cell, and that cell's /forecast lists 12-hour periods; a daytime period's
temperature is the forecast high (whole degrees F) of the local date it starts on. Once a day's daytime period has
passed (after about 6 pm local) the forecast no longer lists that day.

nws_obs: the station's observations of its local STANDARD day so far (a CLI day never follows daylight saving):
the highest and lowest whole degree F, and whether a measured 0.01 inch or more fell in an hour. The day's high only
grows and its low only falls, so an observed value settles part of a daily market before the day ends (the `observed`
family). The IEM ASOS archive backfills the same shape, one reading per observation, so a backtest window exists for
every month it holds.

nws_cli: the climate report of a finished day, parsed by IEM, from `python -m lab.inputs.nws` once a day is `FINAL_H` old.
It is the value Kalshi settles on and the yardstick for the forecast's error (`sd`).

A station that fails is reported and the rest are still read; the run fails if any did, so the CronJob shows it.
"""

import argparse
import csv
import datetime as dt
import io
import statistics
import sys

import requests

from lab import inputs
from lab.core.db import connect
from lab.core.api import Api

NAME = "nws_high"
OBS, CLI = "nws_obs", "nws_cli"
UTC = dt.timezone.utc
RAIN_IN = 0.01  # the least a gauge reports as rain; a trace is not rain (Kalshi: "strictly greater than 0 inches" of measured rain)
MM_IN = 25.4
FINAL_H = 12  # a day's CLI is final by the next morning; it is read once its day ended this long ago
TRACE = 0.0001  # IEM's mark for a trace of precipitation, below RAIN_IN


def highs(forecast: dict) -> dict[str, int]:
    """Local date (ISO) -> forecast high, from a /forecast body's daytime periods."""
    out = {}
    for p in forecast["properties"]["periods"]:
        if p.get("isDaytime") and p.get("temperature") is not None:
            t = float(p["temperature"])
            out[p["startTime"][:10]] = round(t * 9 / 5 + 32 if p.get("temperatureUnit") == "C" else t)
    return out


def read(api: Api, st: dict) -> dict:
    point = api.get(f"/points/{st['lat']:.4f},{st['lon']:.4f}")["properties"]
    body = api.get(f"/gridpoints/{point['gridId']}/{point['gridX']},{point['gridY']}/forecast")
    return {"highs": highs(body), "updated": body["properties"].get("updateTime"),
            "grid": f"{point['gridId']}/{point['gridX']},{point['gridY']}", "tz": point.get("timeZone")}


def client(cfg: dict, session: requests.Session | None = None, accept: str = "application/geo+json") -> Api:
    http = session or requests.Session()
    http.headers.update({"User-Agent": cfg["user_agent"], "Accept": accept})
    return Api("nws" if "weather.gov" in cfg["base_url"] else "iem", cfg, http)


# --- what a station has measured ---------------------------------------------------------------------------------


def local_day(t: dt.datetime, st: dict) -> dt.date:
    """The station's local standard date of an instant."""
    return (t.astimezone(UTC) + dt.timedelta(hours=st["utc_offset"])).date()


def day_start(day: dt.date, st: dict) -> dt.datetime:
    """The UTC instant a station's local standard day begins."""
    return dt.datetime.combine(day, dt.time(), UTC) - dt.timedelta(hours=st["utc_offset"])


def fahrenheit(celsius: float | None) -> int | None:
    return None if celsius is None else round(celsius * 9 / 5 + 32)


def summarize(obs: list[tuple], day: dt.date) -> dict | None:
    """The day so far from its observations (time, whole degrees F or None, inches in the last hour or None), oldest
    first: None before the first one."""
    if not obs:
        return None
    temps = [t for _, t, _ in obs if t is not None]
    return {"day": day.isoformat(), "high": max(temps, default=None), "low": min(temps, default=None),
            "rained": any(p is not None and p >= RAIN_IN for _, _, p in obs), "n": len(obs),
            "last": obs[-1][0].astimezone(UTC).isoformat()}


def station_obs(body: dict) -> list[tuple]:
    """(time, whole degrees F, inches in the last hour), oldest first, from an /observations body."""
    out = []
    for f in body["features"]:
        p = f["properties"]
        mm = (p.get("precipitationLastHour") or {}).get("value")
        out.append((dt.datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")),
                    fahrenheit((p.get("temperature") or {}).get("value")), None if mm is None else mm / MM_IN))
    return sorted(out, key=lambda o: o[0])


def read_obs(api: Api, key: str, st: dict, now: dt.datetime) -> dict | None:
    """The station's local day so far, or None before its first observation of the day."""
    day = local_day(now, st)
    body = api.get(f"/stations/{key}/observations", {"start": day_start(day, st).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                      "limit": 500})
    return summarize(station_obs(body), day)


def asos(api: Api, key: str, st: dict, start: dt.date, end: dt.date) -> list[tuple]:
    """The IEM ASOS archive's observations of the UTC span [start, end), the shape of station_obs."""
    r = api.request("GET", "/cgi-bin/request/asos.py", {
        "station": key[1:], "data": ["tmpf", "p01i"], "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day, "tz": "Etc/UTC", "format": "onlycomma",
        "latlon": "no", "missing": "empty", "trace": TRACE, "report_type": [3, 4]})
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        t = dt.datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        out.append((t, None if row["tmpf"] == "" else round(float(row["tmpf"])),
                    None if row["p01i"] == "" else float(row["p01i"])))
    return sorted(out, key=lambda o: o[0])


def running(obs: list[tuple], st: dict, lag: dt.timedelta):
    """(as_of, summary) for every observation: the day so far as it stood once that observation was served."""
    day, today = None, []
    for o in obs:
        d = local_day(o[0], st)
        if d != day:
            day, today = d, []
        today.append(o)
        yield o[0] + lag, summarize(today, d)


# --- the climate report ------------------------------------------------------------------------------------------


def whole(v) -> int | None:
    return v if isinstance(v, int) else None  # IEM writes "M" for a missing value


def cli_days(api: Api, key: str, year: int) -> list[dict]:
    """The year's climate reports of the station: {day, high, low, precip}, precip in inches (a trace is TRACE)."""
    out = []
    for r in api.get("/json/cli.py", {"station": key, "year": year})["results"]:
        p = r.get("precip")
        out.append({"day": r["valid"], "high": whole(r.get("high")), "low": whole(r.get("low")),
                    "precip": TRACE if p == "T" else p if isinstance(p, (int, float)) else None})
    return out


def run_cli(conn, api: Api, reg: dict, now: dt.datetime, years: list[int] | None = None) -> tuple[int, list[str]]:
    """Writes each station's finished days it has no report for; as_of is the day's end, so a re-read is a no-op."""
    wrote, failed = 0, []
    for key, st in sorted(reg[NAME]["stations"].items()):
        try:
            have = {v["day"] for (v,) in conn.execute("SELECT value FROM input_readings WHERE input = %s AND key = %s",
                                                      (CLI, key))}
            for year in years or [local_day(now, st).year]:
                for d in cli_days(api, key, year):
                    end = day_start(dt.date.fromisoformat(d["day"]) + dt.timedelta(days=1), st)
                    if d["day"] not in have and end + dt.timedelta(hours=FINAL_H) <= now and d["high"] is not None:
                        inputs.write(conn, CLI, key, end, d)
                        wrote += 1
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"nws: {key}: cli: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            failed.append(key)
    return wrote, failed


def forecast_errors(conn, reg: dict) -> dict[str, list[int]]:
    """station -> CLI high minus the forecast high, one per finished day with both: the forecast is the newest read at
    or before the day's local 06:00, so it is the morning's, never one made while the day was already playing out."""
    out = {}
    for key, st in reg[NAME]["stations"].items():
        fc = conn.execute("SELECT as_of, value->'highs' FROM input_readings WHERE input = %s AND key = %s "
                          "ORDER BY as_of", (NAME, key)).fetchall()
        for (v,) in conn.execute("SELECT value FROM input_readings WHERE input = %s AND key = %s", (CLI, key)):
            if v["high"] is None:
                continue
            cut = day_start(dt.date.fromisoformat(v["day"]), st) + dt.timedelta(hours=6)
            morning = [h for t, h in fc if t <= cut and v["day"] in (h or {})]
            if morning:
                out.setdefault(key, []).append(v["high"] - morning[-1][v["day"]])
    return out


def sd_report(errors: dict[str, list[int]]) -> list[str]:
    """One line per station and one pooled: how many days, the mean error and its spread, in degrees F."""
    lines = []
    for key, e in sorted(errors.items()):
        lines.append(f"{key}: {len(e)} days, bias {statistics.fmean(e):+.2f}, sd {statistics.pstdev(e):.2f}")
    pooled = [x for e in errors.values() for x in e]
    if pooled:
        lines.append(f"all: {len(pooled)} days, bias {statistics.fmean(pooled):+.2f}, sd {statistics.pstdev(pooled):.2f}")
    return lines


# --- the runs ----------------------------------------------------------------------------------------------------


def run(conn, api: Api, reg: dict, now: dt.datetime) -> tuple[int, list[str]]:
    """Reads every station's forecast; returns how many were written and the stations that failed."""
    wrote, failed = 0, []
    for key, st in sorted(reg[NAME]["stations"].items()):
        try:
            value = read(api, st)
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"nws: {key}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            failed.append(key)
            continue
        inputs.write(conn, NAME, key, now, value)
        wrote += 1
    return wrote, failed


def run_obs(conn, api: Api, reg: dict, now: dt.datetime) -> tuple[int, list[str]]:
    """Reads every station's day so far; a station with no observation yet today is neither written nor failed."""
    wrote, failed = 0, []
    for key, st in sorted(reg[NAME]["stations"].items()):
        try:
            value = read_obs(api, key, st, now)
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"nws: {key}: obs: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            failed.append(key)
            continue
        if value is not None:
            inputs.write(conn, OBS, key, now, value)
            wrote += 1
    return wrote, failed


def backfill(conn, api: Api, reg: dict, keys: list[str], since: dt.date, until: dt.date) -> int:
    """The ASOS archive of [since, until) as nws_obs readings, a month at a time, and the CLI of the same years."""
    lag = dt.timedelta(minutes=reg[OBS]["lag_min"])
    wrote = 0
    for key in keys:
        st = reg[NAME]["stations"][key]
        start = since - dt.timedelta(days=1)  # the first local day began the UTC day before for a station west of UTC
        while start < until:
            end = min(until, (start.replace(day=1) + dt.timedelta(days=32)).replace(day=1))
            for as_of, value in running(asos(api, key, st, start, end), st, lag):
                if since <= dt.date.fromisoformat(value["day"]) < until:
                    inputs.write(conn, OBS, key, as_of, value)
                    wrote += 1
            start = end
    return wrote


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lab.inputs.nws")
    sub = ap.add_subparsers(dest="cmd")
    bf = sub.add_parser("backfill", help="IEM ASOS and CLI history into input_readings")
    bf.add_argument("--station", action="append", help="a station of the registry (default: all)")
    bf.add_argument("--since", type=dt.date.fromisoformat, required=True)
    bf.add_argument("--until", type=dt.date.fromisoformat, default=dt.datetime.now(UTC).date())
    sub.add_parser("sd", help="the forecast high's error against the CLI high")
    args = ap.parse_args(argv)
    reg = inputs.load()
    now = dt.datetime.now(UTC)
    with connect(autocommit=True) as conn:
        if args.cmd == "sd":
            print("\n".join(sd_report(forecast_errors(conn, reg))) or "nws: no day has both a forecast and a CLI yet")
            return 0
        iem = client(reg[CLI]["api"], accept="application/json")
        if args.cmd == "backfill":
            keys = args.station or sorted(reg[NAME]["stations"])
            n = backfill(conn, iem, reg, keys, args.since, args.until)
            wrote, failed = run_cli(conn, iem, reg, now, list(range(args.since.year, args.until.year + 1)))
            print(f"nws: backfilled {n} observations and {wrote} climate reports, {len(failed)} failed {failed}", flush=True)
            return 1 if failed else 0
        nws = client(reg[NAME]["api"])
        wrote, failed = run(conn, nws, reg, now)
        owrote, ofailed = run_obs(conn, nws, reg, now)
        cwrote, cfailed = run_cli(conn, iem, reg, now)
    print(f"nws: {wrote} forecasts, {owrote} observations, {cwrote} climate reports read; "
          f"failed forecasts {failed}, observations {ofailed}, climate reports {cfailed}", flush=True)
    return 1 if failed or ofailed or cfailed else 0


if __name__ == "__main__":
    sys.exit(main())
