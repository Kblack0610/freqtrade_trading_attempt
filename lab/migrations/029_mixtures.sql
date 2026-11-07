-- Mixtures of strategies scored out of sample, rebuilt by lab/mixtures.py after the book. variant is the md5 of
-- spec (sleeves, weighting, gate), also its `trials` row under strategy 'mixture'. Weights and gates are fit on
-- the months before each scored month. annual is the mean daily return x 365 with a block-bootstrap interval;
-- pbo is across every mixture of the run, so it repeats on each row; weights are the last scored month's.
-- Replaces `portfolio`, the equal-weight pick on in-sample backtest returns.
DROP TABLE portfolio;

CREATE TABLE mixtures (
    variant       text PRIMARY KEY,
    name          text NOT NULL,
    spec          jsonb NOT NULL,
    weighting     text NOT NULL CHECK (weighting IN ('equal', 'inverse_vol', 'hrp')),
    gated         boolean NOT NULL,
    baseline      boolean NOT NULL,
    sleeves       integer NOT NULL,
    months        integer NOT NULL,
    days          integer NOT NULL,
    annual        double precision,
    lo            double precision,
    hi            double precision,
    sharpe        double precision,
    max_drawdown  double precision,
    dsr           double precision,
    trials        integer NOT NULL,
    pbo           double precision,
    weights       jsonb NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now()
);

-- Each mixture's out-of-sample daily return: what lab/scorecard.py splits by market climate.
CREATE TABLE mixture_daily (
    variant  text NOT NULL REFERENCES mixtures ON DELETE CASCADE,
    day      date NOT NULL,
    ret      double precision NOT NULL,
    PRIMARY KEY (variant, day)
);
