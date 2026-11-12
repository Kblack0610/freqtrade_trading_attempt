-- Daily realized return of each strategy x exchange x setup overlay, the overlay twin of strategy_daily, rebuilt by
-- lab/analyze.py derive from backtest_trades. lab/shadow.py marks an overlay shadow account from it.
CREATE TABLE overlay_daily (
    strategy  text NOT NULL,
    exchange  text NOT NULL,
    overlay   text NOT NULL,
    day       date NOT NULL,
    ret       double precision NOT NULL,
    pnl       double precision NOT NULL,
    trades    integer NOT NULL,
    PRIMARY KEY (strategy, exchange, overlay, day)
);
