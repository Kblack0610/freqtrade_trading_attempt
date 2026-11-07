-- Each out-of-sample trade of a walk-forward fold, dated. variant is 'base' (the strategy as it is) or
-- 'tuned' (hyperopt's parameters); both are the test month only, trades straddling the fold boundary
-- already purged. walkforward.*_returns keep the same ratios undated; this is the one dated store, so a
-- daily out-of-sample return series can be built. A fold's rows are replaced when the fold reruns.
CREATE TABLE walkforward_trades (
    strategy     text NOT NULL,
    exchange     text NOT NULL,
    variant      text NOT NULL CHECK (variant IN ('base', 'tuned')),
    fold_start   date NOT NULL,
    pair         text NOT NULL,
    open_date    timestamptz NOT NULL,
    close_date   timestamptz NOT NULL,
    profit_ratio double precision NOT NULL,
    FOREIGN KEY (strategy, exchange, fold_start) REFERENCES walkforward ON DELETE CASCADE
);
CREATE INDEX walkforward_trades_fold ON walkforward_trades (strategy, exchange, fold_start, variant);

-- Summed trade profit ratio per UTC close day: the daily out-of-sample return series of a strategy.
CREATE VIEW walkforward_daily AS
SELECT strategy, exchange, variant, (close_date AT TIME ZONE 'UTC')::date AS day,
       sum(profit_ratio) AS ret, count(*) AS trades
FROM walkforward_trades GROUP BY 1, 2, 3, 4;
