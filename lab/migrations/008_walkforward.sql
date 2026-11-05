-- One walk-forward fold: hyperopt on the training span, then the test month backtested with the
-- strategy as it is (base) and with the tuned parameters, plus the tuned parameters on the training
-- span (in sample). cache_key covers the strategy file, configs, Freqtrade version, dates and the
-- hyperopt settings, so a fold runs once; a failed one is retried until attempts reaches 3.
-- *_returns hold each closed trade's profit ratio, so folds pool into one interval.
CREATE TABLE walkforward (
    strategy         text NOT NULL REFERENCES strategies(name) ON DELETE CASCADE,
    exchange         text NOT NULL,
    fold_start       date NOT NULL,
    train_start      date NOT NULL,
    test_end         date NOT NULL,
    cache_key        text NOT NULL,
    ok               boolean NOT NULL,
    attempts         integer NOT NULL DEFAULT 1,
    error            text,
    epochs           integer,
    loss             text,
    params           jsonb,
    is_trades        integer,
    is_profit        double precision,
    base_trades      integer,
    base_profit      double precision,
    base_expectancy  double precision,
    base_returns     double precision[],
    tuned_trades     integer,
    tuned_profit     double precision,
    tuned_expectancy double precision,
    tuned_returns    double precision[],
    duration_s       double precision,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, fold_start)
);
