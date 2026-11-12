-- lab/calibration.py: the gate stack on synthetic candles with planted edges (edge > 0) and nulls (edge 0).
-- One row per universe (run), strategy and out-of-sample length; `gates` maps each gate's name to whether it passed.
CREATE TABLE calibration_outcomes (
    run             text NOT NULL,
    strategy        text NOT NULL,
    edge            double precision NOT NULL,
    oos_months      integer NOT NULL,
    ready           boolean NOT NULL,  -- every research gate passed (the paper gates and venues agreeing left out)
    gates           jsonb NOT NULL,
    oos_trades      integer,
    oos_expectancy  double precision,
    score           double precision,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run, strategy, oos_months)
);

-- Pass rates over every universe so far, rebuilt from calibration_outcomes each time one is recorded. At edge 0 the
-- rate is the false pass rate, above it the power; gate 'stack' is `ready`. lo and hi: Wilson 95% interval.
CREATE TABLE calibration (
    gate        text NOT NULL,
    edge        double precision NOT NULL,
    oos_months  integer NOT NULL,
    runs        integer NOT NULL,
    n           integer NOT NULL,
    passed      integer NOT NULL,
    rate        double precision NOT NULL,
    lo          double precision,
    hi          double precision,
    PRIMARY KEY (gate, edge, oos_months)
);
