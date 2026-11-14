-- Event-contract walk-forward (lab/markets/families.py): market_backtests keeps one row per test window, a whole
-- settled month keyed by its start, instead of the latest run replacing the last. Each row also keeps each trade's
-- event, for the interval that resamples whole events, and the window's daily returns, for the deflated Sharpe, the
-- minimum backtest length and PBO across windows. The score moves to market_readiness, which pools a variant's
-- windows. Both tables were empty when this ran (2026-11-13), so the key change loses nothing.
ALTER TABLE market_category_results DROP CONSTRAINT market_category_results_family_venue_variant_fkey;
ALTER TABLE market_category_results DROP CONSTRAINT market_category_results_pkey;
ALTER TABLE market_backtests DROP CONSTRAINT market_backtests_pkey;

ALTER TABLE market_backtests DROP COLUMN score;
ALTER TABLE market_backtests ADD COLUMN events text[] NOT NULL DEFAULT '{}';  -- each trade's event, as returns
ALTER TABLE market_backtests ADD COLUMN daily double precision[] NOT NULL DEFAULT '{}';  -- one a day from start_at
ALTER TABLE market_backtests ADD PRIMARY KEY (family, venue, variant, start_at);

ALTER TABLE market_category_results ADD COLUMN start_at timestamptz NOT NULL;
ALTER TABLE market_category_results ADD PRIMARY KEY (family, venue, variant, start_at, category);
ALTER TABLE market_category_results ADD FOREIGN KEY (family, venue, variant, start_at)
    REFERENCES market_backtests ON DELETE CASCADE;

-- The markets readiness gate, one writer: lab/markets/families.py gate(). One row per family, venue and variant over
-- its out-of-sample windows before the lockbox, with suggest.toml [confidence] thresholds; the lockbox months are
-- read only for a variant that passes every other gate.
CREATE TABLE market_readiness (
    family       text NOT NULL,
    venue        text NOT NULL,
    variant      text NOT NULL,
    params       jsonb NOT NULL,
    windows      integer NOT NULL,           -- test windows pooled, the lockbox not counted
    trades       integer NOT NULL,
    expectancy   double precision,
    expectancy_lo double precision,          -- resampled by event
    expectancy_hi double precision,
    dsr          double precision,
    pbo          double precision,           -- the family's variants on the venue, metrics.pbo_panel
    trials       integer NOT NULL,           -- trials on every markets venue when this ran
    ready        boolean NOT NULL,
    passed       integer NOT NULL,
    gates        jsonb NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (family, venue, variant)
);
