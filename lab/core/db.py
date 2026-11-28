"""Postgres connection for the lab: one env var, DATABASE_URL, and the one connect every lab connection goes through.

KEEPALIVE makes a connection notice a server that went away without closing it. On 2026-11-25 asus-laptop rebooted
at 07:03:56Z under Postgres: the jobs that held a connection then (the Kalshi and Polymarket recorders, history-bars)
waited on a reply that could never come, with nothing on the wire to tell them, until their Job deadline killed them,
so no Kalshi sweep ran for 20 minutes and TraderMarketsRecorderStale fired. Keepalives start probing a quiet
connection after 30 s, every 10 s; on Linux tcp_user_timeout then closes it once 60 s pass with no reply, to a probe
or to data sent (keepalives_count is the bound only where the platform has no TCP_USER_TIMEOUT). connect_timeout
bounds a connect to a host that does not answer. The run then fails at once and the next scheduled run starts clean.

A nightly job cannot wait for the next run: walk-forward and analysis hold one connection for hours, and on 2026-11-26
and 11-27 Postgres moved at 01:54 UTC, so the walk-forward's close_shard and the analysis' run update hit a dead
connection and the night's work was never recorded. Those jobs use Reconnecting, which reconnects instead of failing.
"""

import contextlib
import os
import sys
import time

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
    return psycopg.connect(url() if dsn is None else dsn, autocommit=autocommit, **KEEPALIVE)


RECONNECT_S = 600  # how long Reconnecting retries a lost server: a Postgres pod rescheduled to another node is back in minutes


class Reconnecting:
    """An autocommit connection that outlives a Postgres restart. A statement that fails because the connection was
    lost reconnects, with backoff for up to RECONNECT_S, and runs once more; transaction() and cursor() first run a
    probe through the same path, since a connection lost while idle only shows on its next use. Inside a transaction a
    lost connection still raises: the block's earlier statements are gone, and only the caller can redo them.

    The rerun is safe for an upsert, a delete or an update by key, the writes these jobs make. A statement whose reply
    was lost after the server committed it runs twice; for an INSERT ... RETURNING that means one stray row."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn
        self._depth = 0
        self.conn = connect(autocommit=True, dsn=dsn)

    def __enter__(self) -> "Reconnecting":
        return self

    def __exit__(self, *exc) -> None:
        self.conn.close()

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def _reconnect(self) -> None:
        self.conn.close()
        give_up, delay = time.monotonic() + RECONNECT_S, 1.0
        while True:
            try:
                self.conn = connect(autocommit=True, dsn=self._dsn)
                print("db: reconnected to Postgres", file=sys.stderr, flush=True)
                return
            except psycopg.OperationalError:
                if time.monotonic() + delay > give_up:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30.0)

    def execute(self, *args, **kwargs):
        try:
            return self.conn.execute(*args, **kwargs)
        except psycopg.OperationalError as e:
            if self._depth or not self.conn.broken:
                raise
            print(f"db: connection lost ({str(e).strip()[:200]}), reconnecting", file=sys.stderr, flush=True)
            self._reconnect()
            return self.conn.execute(*args, **kwargs)

    @contextlib.contextmanager
    def transaction(self, *args, **kwargs):
        if not self._depth:
            self.execute("SELECT 1")
        self._depth += 1
        try:
            with self.conn.transaction(*args, **kwargs) as tx:
                yield tx
        finally:
            self._depth -= 1

    def cursor(self, *args, **kwargs):
        if not self._depth:
            self.execute("SELECT 1")
        return self.conn.cursor(*args, **kwargs)
