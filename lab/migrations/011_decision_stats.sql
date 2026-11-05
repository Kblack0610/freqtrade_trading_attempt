-- trials is now the effective number of independent trials behind each window's deflated Sharpe, the
-- participation ratio of the correlation matrix of their daily returns; trials_raw is the plain count.
-- trades_needed is how many more trades the window needs before its expectancy interval can clear zero.
ALTER TABLE strategy_windows ALTER COLUMN trials TYPE double precision;
ALTER TABLE strategy_windows ADD COLUMN trials_raw integer;
ALTER TABLE strategy_windows ADD COLUMN trades_needed integer;

-- Probability of backtest overfitting per exchange and window, by combinatorially symmetric
-- cross-validation over the daily returns of every strategy tested there.
CREATE TABLE overfit (
    exchange      text NOT NULL,
    win           text NOT NULL,
    strategies    integer NOT NULL,
    days          integer NOT NULL,
    blocks        integer NOT NULL,
    splits        integer NOT NULL,
    pbo           double precision NOT NULL,
    logit_median  double precision,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, win)
);
