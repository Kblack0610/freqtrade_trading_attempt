-- Per scored strategy and [book] stage: how many of its tuned out-of-sample calendar days (zero where it did not trade)
-- lost the stage [watcher] day_loss of capital, scaling the day's return by stake x max_open / capital. A count of days,
-- rebuilt by lab/crypto/book.py beside `book`.
CREATE TABLE book_days (
    strategy     text NOT NULL,
    exchange     text NOT NULL,
    stage        text NOT NULL,
    days         integer NOT NULL,
    limit_days   integer NOT NULL,
    day_limit    double precision NOT NULL,
    p_day_limit  double precision NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, stage)
);
