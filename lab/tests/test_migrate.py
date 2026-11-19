import pytest

from lab import migrate


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


def test_063_folds_the_stock_ledger_into_the_paper_ledger(conn):
    """A scratch database at migration 062 holding a closed and an open stock position, a decision keyed to the closed
    one: after 063 both are paper_orders of venue alpaca, the decision points at the new order, the old tables are gone."""
    import os

    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"mig063_{os.getpid()}"
    base = conninfo_to_dict(os.environ["DATABASE_URL"])
    try:
        conn.execute(f"CREATE DATABASE {name}")
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("no CREATEDB")
    try:
        with psycopg.connect(make_conninfo(**{**base, "dbname": name}), autocommit=True) as s:
            for f in sorted(migrate.MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
                if f.stem < "063":
                    s.execute(f.read_text())
            s.execute("INSERT INTO stock_accounts (account, family, variant, params, capital, started_at) "
                      "VALUES ('stock-a', 'trend', 'v', '{}', 1000, '2026-01-01')")
            s.execute("INSERT INTO stock_positions (account, ticker, shares, entered_at, quoted_at, entry_price, "
                      "entry_cost, exited_at, exit_price, exit_cost, profit, exit_reason) VALUES "
                      "('stock-a', 'SPY', 2, '2026-01-02', '2026-01-02', 100, 0.1, '2026-01-03', 110, 0.1, 19.8, 'sma'), "
                      "('stock-a', 'TLT', 1.5, '2026-01-04', '2026-01-04', 90, 0.1, NULL, NULL, NULL, NULL, NULL)")
            closed = s.execute("SELECT id FROM stock_positions WHERE ticker = 'SPY'").fetchone()[0]
            s.execute("INSERT INTO decisions (league, venue, account, instrument, decided_at, action, rule, fill_source, "
                      "fill_id) VALUES ('stocks', 'alpaca', 'stock-a', 'SPY', '2026-01-02', 'enter', 'r', 'stock', %s)",
                      (closed,))
            s.execute((migrate.MIGRATIONS / "063_one_paper_ledger.sql").read_text())
            assert not s.execute("SELECT to_regclass('stock_positions'), to_regclass('stock_accounts')").fetchone()[0]
            assert s.execute("SELECT venue, family, capital FROM paper_accounts").fetchall() == [("alpaca", "trend", 1000)]
            got = s.execute("SELECT market_id, side, contracts, price, fee, payout, exit_fee, profit, exit_reason, "
                            "settled_at IS NULL FROM paper_orders ORDER BY id").fetchall()
            assert got == [("SPY", "long", 2, 100, 0.1, 110, 0.1, 19.8, "sma", False),
                           ("TLT", "long", 1.5, 90, 0.1, None, None, None, None, True)]
            assert s.execute("SELECT d.fill_source, o.market_id FROM decisions d JOIN paper_orders o ON o.id = d.fill_id"
                             ).fetchall() == [("paper", "SPY")]
            assert s.execute("SELECT source, venue, instrument, is_open, exit_reason FROM fills WHERE source = 'paper' ORDER BY fill_id"
                             ).fetchall() == [("paper", "alpaca", "SPY", False, "sma"), ("paper", "alpaca", "TLT", True, None)]
    finally:
        conn.execute(f"DROP DATABASE {name} WITH (FORCE)")
