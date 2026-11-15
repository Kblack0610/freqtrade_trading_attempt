"""The decision record's one writer (migration 055): every league's pass calls these in the same transaction as the
order it records, so a fill never exists without its why.

decide() writes one `decisions` row: an entry, an exit or a near-miss, with the rule in words, the checks it ran
({name, value, threshold, pass}), the inputs it read ({name, value, source, as_of}) and the sizing. funnel() writes
the pass's `decision_funnel` counts, one row per (account, reason)."""

import datetime as dt

import pandas as pd
from psycopg.types.json import Jsonb

from lab.util import finite


def jsonable(v):
    """A trace value jsonb can hold: finite numbers, timestamps as ISO strings."""
    if isinstance(v, (dt.datetime, dt.date, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return finite(v)


def decide(conn, league: str, account: str, venue: str, instrument: str, now: dt.datetime, action: str, rule: str,
           inputs: list, checks: list, sizing: dict | None = None, fill: tuple[str, int] | None = None) -> None:
    """One decision; `fill` is its (source, id) in the `fills` view, for the entry that made that fill."""
    source, fill_id = fill or (None, None)
    conn.execute(
        "INSERT INTO decisions (league, account, venue, instrument, decided_at, action, rule, inputs, checks, sizing, "
        "fill_source, fill_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (league, account, venue, instrument, now, action, rule, Jsonb(jsonable(inputs)), Jsonb(jsonable(checks)),
         None if sizing is None else Jsonb(jsonable(sizing)), source, fill_id))


def funnel(conn, league: str, venue: str, now: dt.datetime, counts: dict) -> None:
    """One decision_funnel row per (account, reason) of this pass."""
    conn.cursor().executemany(
        "INSERT INTO decision_funnel (league, account, venue, pass_at, reason, n) VALUES (%s, %s, %s, %s, %s, %s)",
        [(league, a, venue, now, reason, n) for (a, reason), n in sorted(counts.items())])


def prune(conn, league: str, venue: str, keep: dt.datetime) -> None:
    """The trace_days cut: near-misses and funnel rows older than `keep`; entries and exits stay."""
    conn.execute("DELETE FROM decisions WHERE league = %s AND venue = %s AND action = 'near_miss' "
                 "AND decided_at < %s", (league, venue, keep))
    conn.execute("DELETE FROM decision_funnel WHERE league = %s AND venue = %s AND pass_at < %s",
                 (league, venue, keep))
