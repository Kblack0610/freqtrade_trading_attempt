import pytest

import migrate


def test_second_run_is_a_noop(conn, capsys):
    capsys.readouterr()
    assert migrate.main() == 0
    assert "0 new" in capsys.readouterr().out


def test_edited_migration_is_refused(conn):
    real = conn.execute("SELECT sha256 FROM schema_migrations WHERE version = '001_init'").fetchone()[0]
    conn.execute("UPDATE schema_migrations SET sha256 = 'edited' WHERE version = '001_init'")
    try:
        with pytest.raises(SystemExit, match="changed after it was applied"):
            migrate.main()
    finally:
        conn.execute("UPDATE schema_migrations SET sha256 = %s WHERE version = '001_init'", (real,))
