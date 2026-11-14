import os

import pytest

TABLES = ("call_scores, calls, shadow_marks, shadow_accounts, calibration, calibration_outcomes, residual_clusters, residual_venues, venue_budgets, vol_forecasts, research_events, research_state, mixture_daily, mixtures, horizon_verdict, vol_scores, vol_verdict, climate_scorecard, market_category_results, market_readiness, broker_balances, wallet_balances, bankroll, demo_orders, paper_orders, paper_accounts, market_backtests, market_backfill, market_link_asks, market_links, market_sweeps, resolutions, outcomes, markets, venues, benchmarks, book, watch, confidence, walkforward_trades, walkforward, overfit, strategies, backtest_trades, backtest_chunks, overlays, overlay_windows, overlay_daily, strategy_daily, strategy_windows, estimates, regimes, "
          "correlations, bias_checks, suggestions, data_coverage, funding_rates, open_interest, runs, bot_snapshots, bot_trades, bots")


@pytest.fixture
def conn():
    """A migrated, empty trader schema. Needs DATABASE_URL pointing at a throwaway database."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    from lab import db, migrate

    assert migrate.main() == 0
    with db.connect(autocommit=True) as c:
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        yield c
