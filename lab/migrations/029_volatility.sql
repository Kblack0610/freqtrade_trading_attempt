-- lab/volatility.py, rebuilt by every analysis. vol_scores: mean QLIKE of each variance forecast (har, mean, ewma)
-- per venue, pair and test month, over the days all three could be scored. vol_verdict: per venue and baseline,
-- the daily QLIKE difference HAR minus baseline (averaged over pairs) with its block-bootstrap interval; har_wins
-- when its high end is below zero. Sizing may use HAR on a venue only where it wins against every baseline.
CREATE TABLE vol_scores (
    exchange   text NOT NULL,
    pair       text NOT NULL,
    model      text NOT NULL CHECK (model IN ('har', 'mean', 'ewma')),
    fold_start date NOT NULL,
    days       integer NOT NULL,
    qlike      double precision NOT NULL,
    PRIMARY KEY (exchange, pair, model, fold_start)
);

CREATE TABLE vol_verdict (
    exchange       text NOT NULL,
    baseline       text NOT NULL CHECK (baseline IN ('mean', 'ewma')),
    pairs          integer NOT NULL,
    days           integer NOT NULL,
    har_qlike      double precision NOT NULL,
    baseline_qlike double precision NOT NULL,
    diff_lo        double precision,
    diff_hi        double precision,
    har_wins       boolean NOT NULL,
    PRIMARY KEY (exchange, baseline)
);
