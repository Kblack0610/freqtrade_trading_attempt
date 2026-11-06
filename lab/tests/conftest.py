import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLES = ("demo_orders, paper_orders, paper_accounts, market_backtests, market_backfill, market_link_asks, market_links, market_sweeps, resolutions, outcomes, markets, venues, benchmarks, book, watch, confidence, walkforward, overfit, strategies, backtest_trades, backtest_chunks, strategy_daily, strategy_windows, estimates, regimes, "
          "correlations, bias_checks, suggestions, data_coverage, funding_rates, open_interest, runs, bot_snapshots, bot_trades, bots, portfolio")


@pytest.fixture
def conn():
    """A migrated, empty trader schema. Needs DATABASE_URL pointing at a throwaway database."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    import db
    import migrate

    assert migrate.main() == 0
    with db.connect(autocommit=True) as c:
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        yield c
