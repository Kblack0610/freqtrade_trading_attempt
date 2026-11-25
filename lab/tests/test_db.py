"""lab.core.db.connect sets TCP keepalives, and every lab connection goes through it (lab/core/db.py says why)."""

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
