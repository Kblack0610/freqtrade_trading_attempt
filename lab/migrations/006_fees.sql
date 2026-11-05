-- Per-trade expectancy before fees (close over open, spot long) and the mean fee a trade paid,
-- so a strategy's edge can be compared across venues whose fees differ tenfold.
ALTER TABLE strategy_windows
    ADD COLUMN gross_expectancy    double precision,
    ADD COLUMN gross_expectancy_lo double precision,
    ADD COLUMN gross_expectancy_hi double precision,
    ADD COLUMN fee_per_trade       double precision;
