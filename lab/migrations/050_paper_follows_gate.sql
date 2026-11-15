-- The paper league follows the markets readiness gate (lab/markets/paper.py): the league is every ready
-- market_readiness row, and an account whose variant left that set is retired. A retired account takes no entries;
-- its open fills still settle and mark, and it comes back if its variant passes again.
ALTER TABLE paper_accounts ADD COLUMN retired_at timestamptz;

-- The forward check, one writer: paper.forward(). Settled paper per-trade returns against the variant's
-- market_backtests returns, the difference of means bootstrapped (metrics.diff_ci). Null until enough settle.
ALTER TABLE paper_accounts ADD COLUMN paper_trades integer;
ALTER TABLE paper_accounts ADD COLUMN paper_expectancy double precision;
ALTER TABLE paper_accounts ADD COLUMN backtest_trades integer;
ALTER TABLE paper_accounts ADD COLUMN backtest_expectancy double precision;
ALTER TABLE paper_accounts ADD COLUMN gap_lo double precision;   -- paper minus backtest, per trade
ALTER TABLE paper_accounts ADD COLUMN gap_hi double precision;
ALTER TABLE paper_accounts ADD COLUMN drifted boolean;           -- the gap interval excludes zero
ALTER TABLE paper_accounts ADD COLUMN checked_at timestamptz;
