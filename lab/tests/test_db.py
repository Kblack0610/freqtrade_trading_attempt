"""lab.core.db.connect sets TCP keepalives, every lab connection goes through it, and Reconnecting outlives a Postgres restart (lab/core/db.py says why)."""

import ast
import os
import socket
from pathlib import Path

import pytest

from lab.core import db

REPO = Path(__file__).resolve().parents[2]


def test_connect_passes_keepalive(monkeypatch):
    seen = {}
    monkeypatch.setattr(db.psycopg, "connect", lambda dsn, **kw: seen.update(dsn=dsn, **kw))
    db.connect(autocommit=True, dsn="postgresql://x@h/d")
    assert seen == {"dsn": "postgresql://x@h/d", "autocommit": True, **db.KEEPALIVE}


def test_a_live_connection_probes_a_silent_server():
    """libpq applied the options to the real socket: a server that vanishes is noticed in about a minute."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    with db.connect() as conn:
        s = socket.socket(fileno=os.dup(conn.pgconn.socket))
        try:
            if s.family == socket.AF_UNIX:
                pytest.skip("a unix socket has no TCP keepalive")
            assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == db.KEEPALIVE["keepalives_idle"]
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == db.KEEPALIVE["keepalives_interval"]
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == db.KEEPALIVE["keepalives_count"]
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == db.KEEPALIVE["tcp_user_timeout"]
        finally:
            s.close()


POOLS = {"ConnectionPool", "AsyncConnectionPool", "NullConnectionPool", "AsyncNullConnectionPool"}


def name(node) -> str:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


def skips_keepalive(node) -> bool:
    """A psycopg connect other than db.connect, or a pool whose kwargs do not unpack KEEPALIVE."""
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "psycopg" and any(a.name == "connect" for a in node.names)
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "connect":
        return name(f.value) == "psycopg" or name(f.value).endswith("Connection")
    if name(f) in POOLS:
        kw = next((k.value for k in node.keywords if k.arg == "kwargs"), None)
        return not (isinstance(kw, ast.Dict) and any(k is None and name(v) == "KEEPALIVE" for k, v in zip(kw.keys, kw.values)))
    return False


def test_no_connection_skips_keepalive():
    """A bare psycopg connect or a pool without KEEPALIVE would hang on a vanished server again."""
    found = [f"{p.relative_to(REPO)}:{n.lineno}" for top in ("lab", "web", "ci") for p in (REPO / top).rglob("*.py")
             if "tests" not in p.parts and p != REPO / "lab/core/db.py"
             for n in ast.walk(ast.parse(p.read_text())) if skips_keepalive(n)]
    assert found == []


def test_the_guard_sees_each_form():
    """The scan above would pass on nothing if it missed a form, so each one it must catch is checked here."""
    bad = ["psycopg.connect(u)", "from psycopg import connect", "psycopg.AsyncConnection.connect(u)",
           "ConnectionPool(u, kwargs={'autocommit': True})", "AsyncConnectionPool(\n    u,\n)"]
    good = ["db.connect(dsn=u)", "connect(autocommit=True)",
            "ConnectionPool(\n    u,\n    kwargs={'autocommit': True, **db.KEEPALIVE},\n)"]
    assert [c for c in bad if not any(skips_keepalive(n) for n in ast.walk(ast.parse(c)))] == []
    assert [c for c in good if any(skips_keepalive(n) for n in ast.walk(ast.parse(c)))] == []


def kill(conn, r: db.Reconnecting) -> None:
    """What a Postgres restart does to an idle client: the server ends its backend."""
    conn.execute("SELECT pg_terminate_backend(%s)", (r.conn.info.backend_pid,))


def test_reconnecting_reruns_a_statement_on_a_lost_connection(conn):
    with db.Reconnecting() as r:
        before = r.conn.info.backend_pid
        kill(conn, r)
        assert r.execute("SELECT 41 + 1").fetchone()[0] == 42
        assert r.conn.info.backend_pid != before


def test_reconnecting_probes_before_a_transaction_and_a_cursor(conn):
    with db.Reconnecting() as r:
        kill(conn, r)
        with r.transaction():
            r.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('test', 'tx', '{}', now())")
        kill(conn, r)
        with r.cursor() as cur:
            cur.execute("SELECT count(*) FROM runs WHERE name = 'tx'")
            assert cur.fetchone()[0] == 1


def test_reconnecting_does_not_rerun_inside_a_transaction(conn):
    """The block's earlier statements died with the connection, so a rerun of the last one alone would commit half."""
    with db.Reconnecting() as r:
        with pytest.raises(db.psycopg.OperationalError):
            with r.transaction():
                r.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('test', 'half', '{}', now())")
                kill(conn, r)
                r.execute("SELECT 1")
        assert r.execute("SELECT count(*) FROM runs WHERE name = 'half'").fetchone()[0] == 0


def test_reconnecting_gives_up_after_its_budget(conn, monkeypatch):
    with db.Reconnecting() as r:
        kill(conn, r)
        monkeypatch.setattr(db, "RECONNECT_S", 0)
        monkeypatch.setattr(db, "connect", lambda **k: (_ for _ in ()).throw(db.psycopg.OperationalError("down")))
        with pytest.raises(db.psycopg.OperationalError, match="down"):
            r.execute("SELECT 1")


def test_walkforward_records_its_shard_after_postgres_restarted(conn, monkeypatch):
    """2026-11-26 and 11-27: Postgres moved hours into the night and close_shard failed on the dead connection."""
    from lab.crypto import walkforward

    monkeypatch.setattr(walkforward, "SHARDS", 1)
    with db.Reconnecting() as r:
        shard_id = r.execute("INSERT INTO runs (kind, name, raw, started_at) VALUES ('walkforward_shard', 'night#0', '{}', "
                             "now()) RETURNING id").fetchone()[0]
        kill(conn, r)
        rows = walkforward.close_shard(r, shard_id, "night", {"folds_run": 3}, True)
    assert [(ok, raw) for _, ok, raw in rows] == [(True, {"folds_run": 3})]
