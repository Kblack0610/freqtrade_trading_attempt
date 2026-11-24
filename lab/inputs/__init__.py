"""The inputs registry, lab/inputs/inputs.toml, and the readings our own readers write to input_readings (migration 056).

lab/crypto/taxonomy.py checks the registry against lab/crypto/families.toml: each entry's signal_source is one of [signal_sources],
and every crypto strategy's `inputs` names an entry. Families declare theirs in Family.INPUTS (test_families).
"""

import datetime as dt
import tomllib
from pathlib import Path

from psycopg.types.json import Jsonb

CONFIG = Path(__file__).with_name("inputs.toml")
FIELDS = ("title", "signal_source", "origin", "cadence")


def load(path: Path = CONFIG) -> dict:
    """name -> entry. Every entry has FIELDS; a `reader` needs `fresh_min`."""
    reg = tomllib.loads(path.read_text())["inputs"]
    for name, e in reg.items():
        if missing := [f for f in FIELDS if not e.get(f)]:
            raise ValueError(f"input {name} needs {', '.join(missing)}")
        if e.get("reader") and not e.get("fresh_min"):
            raise ValueError(f"input {name} has a reader, so it needs fresh_min")
    return reg


def source(reg: dict, name: str, venue: str) -> str:
    """Where the input comes from, as a decision's input row shows it."""
    return reg[name]["origin"].format(venue=venue)


def write(conn, name: str, key: str, as_of: dt.datetime, value: dict) -> None:
    conn.execute("INSERT INTO input_readings (input, key, as_of, value) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT DO NOTHING", (name, key, as_of, Jsonb(value)))


def readings(conn, name: str, since: dt.datetime, until: dt.datetime) -> list[tuple]:
    """(key, as_of, value) of the input read in [since, until), oldest first."""
    return conn.execute("SELECT key, as_of, value FROM input_readings WHERE input = %s AND as_of >= %s AND as_of < %s "
                        "ORDER BY as_of, key", (name, since, until)).fetchall()


def days(conn, name: str) -> set[dt.date]:
    """The UTC days with at least one reading of the input."""
    return {d for (d,) in conn.execute(
        "SELECT DISTINCT (as_of AT TIME ZONE 'UTC')::date FROM input_readings WHERE input = %s", (name,))}


NEWEST = "SELECT input, max(as_of) AS newest FROM input_readings GROUP BY input"


def freshness(newest: dict, now: dt.datetime, reg: dict | None = None) -> list[dict]:
    """Each input with a reader: its newest reading (from NEWEST, input -> as_of), its limit and whether it is stale."""
    reg = load() if reg is None else reg
    out = []
    for name, e in sorted(reg.items()):
        if not e.get("reader"):
            continue
        t = newest.get(name)
        out.append({"input": name, "title": e["title"], "newest": t, "fresh_min": e["fresh_min"],
                    "stale": t is None or now - t > dt.timedelta(minutes=e["fresh_min"])})
    return out
