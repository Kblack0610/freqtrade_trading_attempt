-- Returns and drawdown are realized on close, so open losses need their own number. min_rate is the
-- lowest candle a trade lived through; worst_open is the deepest any trade in a window went against
-- its entry. Trades stored before this stay NULL until their chunk reruns.
ALTER TABLE backtest_trades ADD COLUMN min_rate double precision;
ALTER TABLE strategy_windows ADD COLUMN worst_open double precision;
