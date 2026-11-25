"""Postgres connection for the lab: one env var, DATABASE_URL, and the one connect every lab connection goes through.

KEEPALIVE makes a connection notice a server that went away without closing it. On 2026-11-25 asus-laptop rebooted
at 07:03:56Z under Postgres: the jobs that held a connection then (the Kalshi and Polymarket recorders, history-bars)
waited on a reply that could never come, with nothing on the wire to tell them, until their Job deadline killed them,
so no Kalshi sweep ran for 20 minutes and TraderMarketsRecorderStale fired. With TCP keepalives idle 30 s, probed
every 10 s, 3 misses, a waiting connection errors about 60 s after the server is gone; tcp_user_timeout bounds data
sent and never acknowledged the same way (keepalives do not probe while it is outstanding); connect_timeout bounds a
connect to a server that does not answer. The run then fails at once and the next scheduled run starts clean.
"""

import os
import sys

import psycopg

KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
             "tcp_user_timeout": 60_000, "connect_timeout": 10}


def url() -> str:
    found = os.environ.get("DATABASE_URL")
    if not found:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)
    return found


def connect(autocommit: bool = False, dsn: str | None = None) -> psycopg.Connection:
    """A connection to `dsn`, by default DATABASE_URL, with KEEPALIVE set."""
    return psycopg.connect(dsn or url(), autocommit=autocommit, **KEEPALIVE)
