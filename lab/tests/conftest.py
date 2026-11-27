import os

import pytest

TABLES = ("input_readings, decisions, decision_funnel, call_rule_events, call_scores, calls, shadow_marks, shadow_accounts, calibration, calibration_outcomes, residual_clusters, residual_venues, venue_budgets, vol_forecasts, research_events, research_state, mixture_daily, mixtures, horizon_verdict, vol_scores, vol_verdict, climate_scorecard, market_category_results, readiness, broker_balances, wallet_balances, bankroll, demo_orders, paper_orders, paper_accounts, market_backtests, market_backfill, market_link_asks, market_links, market_sweeps, resolutions, outcomes, markets, venues, benchmarks, book, book_stops, watch, walkforward_trades, walkforward, overfit, strategies, backtest_trades, backtest_chunks, overlays, overlay_windows, overlay_daily, strategy_daily, strategy_windows, estimates, regimes, "
          "correlations, bias_checks, suggestions, data_coverage, pair_windows, pair_listings, funding_rates, open_interest, runs, bot_snapshots, bot_trades, bots")


@pytest.fixture
def conn():
    """A migrated, empty trader schema. Needs DATABASE_URL pointing at a throwaway database."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    from lab.core import db
    from lab import migrate

    assert migrate.main() == 0
    with db.connect(autocommit=True) as c:
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        yield c


def pytest_collection_modifyitems(config, items):
    """CI runs the suite in TEST_SHARDS parallel jobs: this one keeps every TEST_SHARDS-th test from TEST_SHARD.

    Round-robin over the collected order, so the shards stay even as tests are added with no weights file to drift.
    Unset, every test runs. Each shard has its own database, so the schema truncation in `conn` cannot cross shards.
    """
    shards = int(os.environ.get("TEST_SHARDS", "1"))
    if shards < 2:
        return
    try:
        shard = int(os.environ["TEST_SHARD"])
    except (KeyError, ValueError):
        raise pytest.UsageError("TEST_SHARDS is set, so TEST_SHARD must be an integer")
    if not 0 <= shard < shards:
        raise pytest.UsageError(f"TEST_SHARD {shard} outside 0..{shards - 1}")
    kept = items[shard::shards]
    keep = set(kept)
    config.hook.pytest_deselected(items=[i for i in items if i not in keep])
    items[:] = kept
