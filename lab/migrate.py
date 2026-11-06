#!/usr/bin/env python3
"""Apply lab/migrations/NNN_*.sql to DATABASE_URL, in order, each once.

Runs as the init container of every job, so it must be a no-op when nothing is new and
safe when two jobs start at once (a session advisory lock serializes them). Each file runs
in its own transaction with its schema_migrations row. A file whose content changed after it
was applied is an error: add the next number instead of editing history.
"""

import hashlib
import sys
from pathlib import Path

from lab.db import connect

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 7_270_001  # any constant; only this script takes it


def pending(applied: dict[str, str]) -> list[tuple[str, str, str]]:
    out = []
    for f in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        sql = f.read_text()
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if f.stem in applied:
            if applied[f.stem] != digest:
                sys.exit(f"migrate: {f.name} changed after it was applied; add a new migration instead")
            continue
        out.append((f.stem, digest, sql))
    return out


def main() -> int:
    with connect(autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            version text PRIMARY KEY,
                            sha256 text NOT NULL,
                            applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = dict(conn.execute("SELECT version, sha256 FROM schema_migrations").fetchall())
        todo = pending(applied)
        for version, digest, sql in todo:
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version, sha256) VALUES (%s, %s)", (version, digest))
            print(f"migrate: applied {version}")
        print(f"migrate: {len(applied) + len(todo)} applied, {len(todo)} new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
