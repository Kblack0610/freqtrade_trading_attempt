#!/usr/bin/env python3
"""The NWS forecast high of each station in lab/inputs.toml [inputs.nws_high.stations], one input_readings row each.

  python -m lab.nws     # the hourly CronJob (home-config trader-inputs-nws)

api.weather.gov, free and keyless, through lab/markets/api.py's paced client. For each station: /points/<lat>,<lon>
names its forecast grid cell, and that cell's /forecast lists 12-hour periods; a daytime period's temperature is the
forecast high (whole degrees F) of the local date it starts on. Once a day's daytime period has passed (after about
6 pm local) the forecast no longer lists that day. A station that fails is reported and the rest are still read;
the run fails if any did, so the CronJob shows it.
"""

import datetime as dt
import sys

import requests

from lab import inputs
from lab.db import connect
from lab.markets.api import Api

NAME = "nws_high"


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


def client(cfg: dict, session: requests.Session | None = None) -> Api:
    http = session or requests.Session()
    http.headers.update({"User-Agent": cfg["user_agent"], "Accept": "application/geo+json"})
    return Api("nws", cfg, http)


def run(conn, api: Api, reg: dict, now: dt.datetime) -> tuple[int, list[str]]:
    """Reads every station; returns how many were written and the stations that failed."""
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


def main(argv: list[str] | None = None) -> int:
    reg = inputs.load()
    api = client(reg[NAME]["api"])
    with connect(autocommit=True) as conn:
        wrote, failed = run(conn, api, reg, dt.datetime.now(dt.timezone.utc))
    print(f"nws: {wrote} stations read, {len(failed)} failed {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
