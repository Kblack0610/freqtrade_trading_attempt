#!/usr/bin/env python3
"""The nightly research loop's last step: what changed since the last run, as events for the agent board.

Nightly, walk-forward (01:00) adds folds and analysis (02:45) rescores: backtests, suggestions, confidence, book,
mixtures, the climate scorecard. This step reads the result. It holds two sets: every strategy or mixture whose
climate_scorecard cell has its low end above zero (signal climate) and every open promote suggestion (signal
promote). Each member that entered or left a set since the last run becomes a `research_events` row, and the set
is stored in `research_state` for the next run. Qualified monthly retune fits join the set, keyed by their
action digest (training dates and parameters), so a new fit is reported once, even if the parameters match.
The betting lane adds two sets: every event-contract variant whose readiness row is ready (signal bet-ready,
written by lab/events/families.py) and every unretired paper account whose forward check drifted from its
backtest (signal bet-drift, written by lab/events/paper.py). Their events carry the betting lane's trial count,
the others the crypto lab's. The first run has no previous set, so everything passing enters.

ci/research_sync.py, on the fleet machine, reads the unreported events (`python -m lab.crypto.loop pending`), files
the promotion, retune and betting ones on the agent board with the avenues register link (a promotion gets a board
row, no roster PR; a climate change gets none, /book shows it) and marks them all reported
(`python -m lab.crypto.loop ack <id>...`). Nothing here trades or edits the roster.
"""

import hashlib
import json
import sys

from lab.crypto import confidence, suggest
from lab.core.db import connect
from lab.events import backtest
from lab.stages.recorder import load_config

AVENUES = "https://github.com/Kblack0610/freqtrade_trading_attempt/blob/master/docs/avenues.md"

KEY = ("signal", "kind", "name", "exchange", "variant", "cell")
CLIMATE = ("SELECT 'climate', kind, name, exchange, variant, cell, lo, annual, '' FROM climate_scorecard "
           "WHERE kind IN ('strategy', 'mixture') AND lo > 0")
PROMOTE = "SELECT strategy, exchange FROM suggestions WHERE kind = 'promote'"
BET_READY = ("SELECT 'bet-ready', 'family', family, venue, variant, '', expectancy_lo, NULL::float8, params::text "
             "FROM readiness WHERE market = 'events' AND ready")
BET_DRIFT = ("SELECT 'bet-drift', 'paper', family, venue, variant, account, gap_lo, NULL::float8, "
             "format('paper %s per trade over %s settled, backtest %s over %s, gap %s to %s', round(paper_expectancy::numeric, 4), "
             "paper_trades, round(backtest_expectancy::numeric, 4), backtest_trades, round(gap_lo::numeric, 4), round(gap_hi::numeric, 4)) "
             "FROM paper_accounts WHERE drifted AND retired_at IS NULL")
BETTING = ("bet-ready", "bet-drift")  # signals scored against the markets trial register, not the crypto one
STATE_COLS = KEY + ("lo", "annual", "action")
EVENT_COLS = ("signal", "change", "kind", "name", "exchange", "variant", "cell", "lo", "annual", "trials", "action")


def current(conn) -> dict[tuple, dict]:
    rows = list(conn.execute(CLIMATE))
    rows += [("promote", "", s, e, "", "", None, None, suggest.roster_line(s, e)) for s, e in conn.execute(PROMOTE)]
    rows += [("retune", "strategy", s, e, hashlib.sha256(action.encode()).hexdigest(), "", None, None, action)
             for s, e, action in conn.execute("SELECT strategy, exchange, action FROM suggestions WHERE kind = 'retune'")]
    rows += list(conn.execute(BET_READY)) + list(conn.execute(BET_DRIFT))
    return {r[:6]: dict(zip(STATE_COLS, r)) for r in rows}


def diff(before: dict[tuple, dict], now: dict[tuple, dict], trials: int, bet_trials: int) -> list[dict]:
    """Pure: an event per key in one set and not the other, entries first, in key order, each with its lane's
    trial count."""
    def event(row, change):
        return dict(row, change=change, trials=bet_trials if row["signal"] in BETTING else trials)
    out = [event(now[k], "enters") for k in sorted(now.keys() - before.keys())]
    out += [event(before[k], "leaves") for k in sorted(before.keys() - now.keys())]
    return out


def run(conn) -> dict:
    with conn.transaction():
        before = {r[:6]: dict(zip(STATE_COLS, r)) for r in conn.execute(f"SELECT {', '.join(STATE_COLS)} FROM research_state")}
        now = current(conn)
        events = diff(before, now, confidence.trial_count(conn), backtest.trial_count(conn, load_config()))
        conn.execute("DELETE FROM research_state")
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO research_state ({', '.join(STATE_COLS)}) "
                            f"VALUES ({', '.join(f'%({k})s' for k in STATE_COLS)})", list(now.values()))
            cur.executemany(f"INSERT INTO research_events ({', '.join(EVENT_COLS)}) "
                            f"VALUES ({', '.join(f'%({k})s' for k in EVENT_COLS)})", events)
    return {"research_passing": sum(k[0] == "climate" for k in now),
            "research_enters": sum(e["change"] == "enters" for e in events),
            "research_leaves": sum(e["change"] == "leaves" for e in events),
            "bet_ready": sum(k[0] == "bet-ready" for k in now), "bet_drifted": sum(k[0] == "bet-drift" for k in now)}


def pending(conn) -> list[dict]:
    cols = ("id", "at") + EVENT_COLS
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM research_events WHERE reported_at IS NULL ORDER BY id")
    return [dict(zip(cols, r), at=r[1].isoformat(), avenue=AVENUES) for r in rows]


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
            print("usage: python -m lab.crypto.loop [pending | ack <id>...]", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
