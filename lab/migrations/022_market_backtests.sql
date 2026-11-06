-- Event-contract backtests, one writer: lab/markets/backtest.py run(). One row per family, venue and variant, the
-- latest run replacing the last; each variant is also a row of the trial register, `trials`, written before its
-- result is read.
CREATE TABLE market_backtests (
    family         text NOT NULL,
    venue          text NOT NULL,
    variant        text NOT NULL,             -- md5 of params, as in trials
    params         jsonb NOT NULL,
    start_at       timestamptz NOT NULL,
    end_at         timestamptz NOT NULL,
    orders         integer NOT NULL,
    fills          integer NOT NULL,
    trades         integer NOT NULL,          -- fills settled by a resolution
    open_fills     integer NOT NULL,          -- fills whose market had not resolved
    metrics        jsonb NOT NULL,            -- metrics.window_metrics over the run
    returns        double precision[] NOT NULL, -- each trade's net return, in settlement order: what the score reads
    expectancy     double precision,
    expectancy_lo  double precision,          -- block-bootstrap interval, suggest.toml [confidence] expectancy_level
    expectancy_hi  double precision,
    dsr            double precision,          -- deflated Sharpe at `trials`
    trials         integer NOT NULL,          -- trials on every markets venue when this ran
    score          double precision,          -- confidence.scores over every row here; null outside the population
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (family, venue, variant)
);
