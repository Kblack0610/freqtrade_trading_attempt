-- lab/horizon.py, rebuilt by every analysis. Per strategy and variant, its out-of-sample days against two passive
-- plans over the same days: hold BTC and BTC while above its long mean. diff_* is the block-bootstrap interval of the
-- mean daily difference (strategy minus plan) x 365; verdict is beats (lo above zero), loses (hi below zero) or open.
CREATE TABLE horizon_verdict (
    strategy  text NOT NULL,
    exchange  text NOT NULL,
    variant   text NOT NULL,
    plan      text NOT NULL CHECK (plan IN ('hold', 'trend')),
    days      integer NOT NULL,
    strat_annual double precision NOT NULL,
    plan_annual  double precision NOT NULL,
    diff_lo   double precision,
    diff_hi   double precision,
    verdict   text NOT NULL CHECK (verdict IN ('beats', 'loses', 'open')),
    PRIMARY KEY (strategy, exchange, variant, plan)
);
