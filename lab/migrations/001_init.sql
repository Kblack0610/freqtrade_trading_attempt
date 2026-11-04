-- Live league data (bots, trades, snapshots) and research results (runs and their rows).
-- Applied once by lab/migrate.py; never edit an applied file, add the next number instead.

CREATE TABLE bots (
    name        text PRIMARY KEY,
    exchange    text NOT NULL,
    strategy    text NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);

-- One row per trade per bot. trade_id restarts at 1 if a bot's sqlite is ever lost,
-- so open_date is part of the key and an old life's trades are kept, not overwritten.
CREATE TABLE bot_trades (
    bot           text NOT NULL REFERENCES bots (name),
    trade_id      integer NOT NULL,
    open_date     timestamptz NOT NULL,
    pair          text NOT NULL,
    is_open       boolean NOT NULL,
    close_date    timestamptz,
    open_rate     double precision,
    close_rate    double precision,
    amount        double precision,
    stake_amount  double precision,
    profit_ratio  double precision,
    profit_abs    double precision,
    exit_reason   text,
    enter_tag     text,
    raw           jsonb NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot, trade_id, open_date)
);
CREATE INDEX bot_trades_close_date ON bot_trades (bot, close_date);

-- One row per bot per ingest run (hourly), or per day for rows imported from
-- league-history.jsonl (source = 'report'). error is set when the bot was unreachable.
CREATE TABLE bot_snapshots (
    bot                text NOT NULL REFERENCES bots (name),
    ts                 timestamptz NOT NULL,
    source             text NOT NULL DEFAULT 'ingest',
    dry_run            boolean,
    closed_trades      integer,
    open_trades        integer,
    profit_closed_pct  double precision,
    profit_all_pct     double precision,
    profit_closed_abs  double precision,
    profit_all_abs     double precision,
    winrate_pct        double precision,
    max_drawdown_pct   double precision,
    profit_factor      double precision,
    balance_total      double precision,
    bot_start          timestamptz,
    error              text,
    raw                jsonb,
    PRIMARY KEY (bot, ts)
);
CREATE INDEX bot_snapshots_ts ON bot_snapshots (ts);

-- One research run (today: one weekly experiments.sh pass, keyed by its run dir name).
CREATE TABLE runs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,
    name         text NOT NULL,
    imported_at  timestamptz NOT NULL DEFAULT now(),
    raw          jsonb NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE backtests (
    run_id            bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    exchange          text NOT NULL,
    strategy          text NOT NULL,
    timeframe         text,
    trades            integer,
    profit_pct        double precision,
    winrate_pct       double precision,
    max_drawdown_pct  double precision,
    profit_factor     double precision,
    sharpe            double precision,
    start_date        timestamptz,
    end_date          timestamptz,
    lookahead_bias    boolean,
    error             text,
    PRIMARY KEY (run_id, exchange, strategy)
);
CREATE INDEX backtests_strategy ON backtests (strategy, exchange);

CREATE TABLE bias_checks (
    run_id             bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    strategy           text NOT NULL,
    kind               text NOT NULL,
    has_bias           boolean NOT NULL,
    biased_indicators  text,
    PRIMARY KEY (run_id, strategy, kind)
);

CREATE TABLE hyperopt_runs (
    run_id    bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    strategy  text NOT NULL,
    result    jsonb NOT NULL,
    PRIMARY KEY (run_id, strategy)
);
