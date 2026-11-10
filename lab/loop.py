#!/usr/bin/env python3
"""The nightly research loop's last step: what changed since the last run, as events for the agent board.

Nightly, walk-forward (01:00) adds folds and analysis (02:45) rescores: backtests, suggestions, confidence, book,
mixtures, the climate scorecard. This step reads the result. It holds two sets: every strategy or mixture whose
climate_scorecard cell has its low end above zero (signal climate) and every open promote suggestion (signal
promote). Each member that entered or left a set since the last run becomes a `research_events` row, and the set
is stored in `research_state` for the next run. Qualified monthly retune fits join the set, keyed by their
action digest (training dates and parameters), so a new fit is reported once, even if the parameters match.
The first run has no previous set, so everything passing enters.

ci/research_sync.py, on the fleet machine, reads the unreported events (`python -m lab.loop pending`), files
them on the agent board, opens a paper-only roster PR for a promotion, and marks them reported
(`python -m lab.loop ack <id>...`). Nothing here trades or edits the roster.
"""

import hashlib
import json
import sys

from lab import suggest
from lab.db import connect

KEY = ("signal", "kind", "name", "exchange", "variant", "cell")
CLIMATE = ("SELECT 'climate', kind, name, exchange, variant, cell, lo, annual, '' FROM climate_scorecard "
           "WHERE kind IN ('strategy', 'mixture') AND lo > 0")
PROMOTE = "SELECT strategy, exchange FROM suggestions WHERE kind = 'promote'"
STATE_COLS = KEY + ("lo", "annual", "action")
EVENT_COLS = ("signal", "change", "kind", "name", "exchange", "variant", "cell", "lo", "annual", "trials", "action")


def current(conn) -> dict[tuple, dict]:
    rows = list(conn.execute(CLIMATE))
    rows += [("promote", "", s, e, "", "", None, None, suggest.roster_line(s, e)) for s, e in conn.execute(PROMOTE)]
    rows += [("retune", "strategy", s, e, hashlib.sha256(action.encode()).hexdigest(), "", None, None, action)
             for s, e, action in conn.execute("SELECT strategy, exchange, action FROM suggestions WHERE kind = 'retune'")]
    return {r[:6]: dict(zip(STATE_COLS, r)) for r in rows}


def diff(before: dict[tuple, dict], now: dict[tuple, dict], trials: int) -> list[dict]:
    """Pure: an event per key in one set and not the other, entries first, in key order."""
    out = [dict(now[k], change="enters", trials=trials) for k in sorted(now.keys() - before.keys())]
    out += [dict(before[k], change="leaves", trials=trials) for k in sorted(before.keys() - now.keys())]
    return out


def run(conn) -> dict:
    with conn.transaction():
        before = {r[:6]: dict(zip(STATE_COLS, r)) for r in conn.execute(f"SELECT {', '.join(STATE_COLS)} FROM research_state")}
        now = current(conn)
        trials = conn.execute("SELECT count(*) FROM trials").fetchone()[0]
        events = diff(before, now, trials)
        conn.execute("DELETE FROM research_state")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO research_state ({', '.join(STATE_COLS)}) "
                            f"VALUES ({', '.join(f'%({k})s' for k in STATE_COLS)})", list(now.values()))
            cur.executemany(f"INSERT INTO research_events ({', '.join(EVENT_COLS)}) "
                            f"VALUES ({', '.join(f'%({k})s' for k in EVENT_COLS)})", events)
    return {"research_passing": sum(k[0] == "climate" for k in now),
            "research_enters": sum(e["change"] == "enters" for e in events),
            "research_leaves": sum(e["change"] == "leaves" for e in events)}


def pending(conn) -> list[dict]:
    cols = ("id", "at") + EVENT_COLS
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM research_events WHERE reported_at IS NULL ORDER BY id")
    return [dict(zip(cols, r), at=r[1].isoformat()) for r in rows]


def ack(conn, ids: list[int]) -> int:
    return conn.execute("UPDATE research_events SET reported_at = now() WHERE id = ANY(%s) AND reported_at IS NULL",
                        (ids,)).rowcount


def main(argv: list[str]) -> int:
    with connect(autocommit=True) as conn:
        if argv[:1] == ["pending"]:
            print(json.dumps(pending(conn)))
        elif argv[:1] == ["ack"] and argv[1:]:
            print(f"loop: {ack(conn, [int(a) for a in argv[1:]])} reported")
        elif not argv:
            print(f"loop: {run(conn)}", flush=True)
        else:
            print("usage: python -m lab.loop [pending | ack <id>...]", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
