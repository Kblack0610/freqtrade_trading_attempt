-- The up-to-three bots Stage 2 could run together, rebuilt by lab/book.py after every score. Each row is one member;
-- worst_corr is its highest daily return correlation with another member, and combined_drawdown is the equal-weight
-- set's, so it repeats on every row.
CREATE TABLE portfolio (
    strategy           text NOT NULL,
    exchange           text NOT NULL,
    worst_corr         double precision NOT NULL,
    alone_drawdown     double precision NOT NULL,
    combined_drawdown  double precision NOT NULL,
    days               integer NOT NULL,
    PRIMARY KEY (strategy, exchange)
);
