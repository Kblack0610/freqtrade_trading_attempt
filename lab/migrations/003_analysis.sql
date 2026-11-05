-- The analysis engine (lab/analyze.py). Every strategy is backtested on each exchange's full
-- history in yearly chunks, and everything else is derived from the trades those chunks store.
-- It replaces the weekly experiments run, so that run's tables and rows go.

DROP TABLE backtests;
DROP TABLE bias_checks;
DROP TABLE hyperopt_runs;
DELETE FROM runs WHERE kind = 'experiments';

-- runs is now the analysis job log: one row per lab/analyze.py run, raw holds its counts. Its
-- name is the start time, which two runs can share, so (kind, name) is no longer unique.
ALTER TABLE runs DROP CONSTRAINT runs_kind_name_key;
ALTER TABLE runs ADD COLUMN started_at timestamptz, ADD COLUMN finished_at timestamptz, ADD COLUMN ok boolean;

-- Every strategy file the resolver found on the last run.
CREATE TABLE strategies (
    name                  text PRIMARY KEY,
    file                  text NOT NULL,
    file_hash             text NOT NULL,
    timeframe             text,
    startup_candle_count  integer,
    load_error            text,
    seen_at               timestamptz NOT NULL DEFAULT now()
);

-- One backtest of one strategy on one exchange over one calendar year (the current year runs to
-- today). cache_key covers the strategy file, the config files, the Freqtrade version and the
-- range, so a past year is backtested once and reruns only when one of those changes. A failed
-- chunk keeps its key and is retried until attempts reaches 3.
CREATE TABLE backtest_chunks (
    id                bigserial PRIMARY KEY,
    strategy          text NOT NULL,
    exchange          text NOT NULL,
    chunk_start       date NOT NULL,
    chunk_end         date NOT NULL,
    cache_key         text NOT NULL,
    ok                boolean NOT NULL,
    attempts          integer NOT NULL DEFAULT 1,
    error             text,
    bt_start          timestamptz,
    bt_end            timestamptz,
    trades            integer,
    start_balance     double precision,
    final_balance     double precision,
    max_drawdown      double precision,
    duration_s        double precision,
    run_id            bigint REFERENCES runs (id) ON DELETE SET NULL,
    ran_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (strategy, exchange, chunk_start)
);

CREATE TABLE backtest_trades (
    chunk_id      bigint NOT NULL REFERENCES backtest_chunks (id) ON DELETE CASCADE,
    pair          text NOT NULL,
    open_date     timestamptz NOT NULL,
    close_date    timestamptz NOT NULL,
    open_rate     double precision,
    close_rate    double precision,
    stake_amount  double precision,
    profit_ratio  double precision NOT NULL,
    profit_abs    double precision NOT NULL,
    exit_reason   text,
    enter_tag     text
);
CREATE INDEX backtest_trades_chunk ON backtest_trades (chunk_id, close_date);

-- Daily realized return of each strategy x exchange, zero days included, chained across chunks.
CREATE TABLE strategy_daily (
    strategy  text NOT NULL,
    exchange  text NOT NULL,
    day       date NOT NULL,
    ret       double precision NOT NULL,
    pnl       double precision NOT NULL,
    trades    integer NOT NULL,
    PRIMARY KEY (strategy, exchange, day)
);

-- Metrics over the trailing 30d, 90d, 180d, 365d and the whole history. Fractions, not percents.
-- dsr is the deflated Sharpe: the probability the Sharpe beats luck among every strategy x
-- exchange in the same window.
CREATE TABLE strategy_windows (
    strategy           text NOT NULL,
    exchange           text NOT NULL,
    win                text NOT NULL,
    days               integer NOT NULL,
    start_day          date,
    end_day            date,
    trades             integer NOT NULL,
    total_return       double precision,
    cagr               double precision,
    sharpe             double precision,
    sharpe_lo          double precision,
    sharpe_hi          double precision,
    sortino            double precision,
    calmar             double precision,
    max_drawdown       double precision,
    profit_factor      double precision,
    expectancy         double precision,
    expectancy_lo      double precision,
    expectancy_hi      double precision,
    winrate            double precision,
    exposure           double precision,
    months_profitable  double precision,
    dsr                double precision,
    trials             integer,
    computed_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, win)
);

-- Seeded Monte Carlo bands from a block bootstrap of the last 365 days of daily returns.
CREATE TABLE estimates (
    strategy          text NOT NULL,
    exchange          text NOT NULL,
    horizon_days      integer NOT NULL,
    p10               double precision NOT NULL,
    p50               double precision NOT NULL,
    p90               double precision NOT NULL,
    prob_loss         double precision NOT NULL,
    exp_max_drawdown  double precision NOT NULL,
    paths             integer NOT NULL,
    seed              bigint NOT NULL,
    basis_days        integer NOT NULL,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, horizon_days)
);

-- BTC/USD market regime per day (from REGIME_EXCHANGE's 1d candles).
CREATE TABLE regimes (
    day         date PRIMARY KEY,
    btc_close   double precision NOT NULL,
    trend       text,
    vol_30d     double precision,
    vol_bucket  text
);

-- Correlation of daily returns over the last 365 days with each strategy the league runs,
-- both backtested on the same exchange.
CREATE TABLE correlations (
    strategy  text NOT NULL,
    exchange  text NOT NULL,
    other     text NOT NULL,
    rho       double precision NOT NULL,
    days      integer NOT NULL,
    PRIMARY KEY (strategy, exchange, other)
);

-- lookahead-analysis and recursive-analysis per strategy, rerun when its file changes.
-- has_bias is NULL when the check could not run (error says why).
CREATE TABLE bias_checks (
    strategy           text NOT NULL,
    kind               text NOT NULL,
    file_hash          text NOT NULL,
    has_bias           boolean,
    biased_indicators  text,
    detail             jsonb,
    error              text,
    checked_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, kind)
);
