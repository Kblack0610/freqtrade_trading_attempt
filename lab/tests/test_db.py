"""lab.core.db.connect sets TCP keepalives, and every lab connection goes through it (lab/core/db.py says why)."""

import os
import re
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
        finally:
            s.close()


def test_no_connection_skips_keepalive():
    """A bare psycopg.connect or a pool without KEEPALIVE would hang on a vanished server again."""
    found = [f"{p.relative_to(REPO)}:{i}" for top in ("lab", "web", "ci") for p in (REPO / top).rglob("*.py")
             if "tests" not in p.parts and p != REPO / "lab/core/db.py"
             for i, line in enumerate(p.read_text().splitlines(), 1)
             if re.search(r"psycopg\.connect\(|ConnectionPool\((?!.*KEEPALIVE)", line)]
    assert found == []
