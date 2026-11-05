-- The trial register: everything ever tried, kept when a strategy file goes, so the bar a deflated
-- Sharpe has to clear never falls. variant is 'as-is', 'load-failed' or a hash of tuned parameters.
CREATE TABLE trials (
    strategy    text NOT NULL,
    exchange    text NOT NULL,
    variant     text NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, variant)
);
-- Three hyperopts ran before migration 003 dropped hyperopt_runs. Their rows are gone, their tries still count.
INSERT INTO trials (strategy, exchange, variant) VALUES
    ('(dropped)', '', 'hyperopt-1'), ('(dropped)', '', 'hyperopt-2'), ('(dropped)', '', 'hyperopt-3');

-- Whether a strategy on an exchange has earned real money, rebuilt by lab/confidence.py after every analysis
-- and walk-forward. score is 100 x P(true net expectancy per trade > 0), NULL with no out-of-sample result to
-- shrink; gates holds every gate's value and threshold; ready means all of them pass.
CREATE TABLE confidence (
    strategy        text NOT NULL,
    exchange        text NOT NULL,
    score           double precision,
    ready           boolean NOT NULL,
    passed          integer NOT NULL,
    oos_trades      integer NOT NULL,
    oos_expectancy  double precision,
    gates           jsonb NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange)
);
