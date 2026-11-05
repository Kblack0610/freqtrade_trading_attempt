-- Each league bot's dry run against a backtest of the same strategy, exchange and period, rebuilt by
-- every analysis run. The gap is how far the backtest's fill and fee model is off from what the bot does.
CREATE TABLE drift (
    bot              text PRIMARY KEY REFERENCES bots(name) ON DELETE CASCADE,
    strategy         text NOT NULL,
    exchange         text NOT NULL,
    since            timestamptz NOT NULL,
    bt_start         timestamptz,
    bt_end           timestamptz,
    live_entries     integer,
    bt_entries       integer,
    matched          integer,
    live_closed      integer,
    bt_closed        integer,
    live_expectancy  double precision,
    bt_expectancy    double precision,
    live_profit      double precision,
    bt_profit        double precision,
    gap              double precision,
    gap_trades       integer,
    error            text,
    computed_at      timestamptz NOT NULL DEFAULT now()
);
