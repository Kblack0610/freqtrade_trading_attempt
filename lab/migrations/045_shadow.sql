-- Shadow paper accounts: one writer, lab/shadow.py, a step of the nightly analysis. A strategy x venue that clears
-- the walk-forward entry gates (lab/suggest.toml [shadow]) gets an account with paper capital and no pod; each night
-- appends the days after its promotion from strategy_daily, and a written rule marks it winning or retires it.
-- Marks are appended, never recomputed, so a later backtest rerun cannot rewrite what an account already showed.
CREATE TABLE shadow_accounts (
    account      text PRIMARY KEY,              -- shadow-<strategy>-<exchange>[-<overlay>]
    strategy     text NOT NULL,
    exchange     text NOT NULL,
    overlay      text NOT NULL DEFAULT '',      -- '' for the strategy as it stands, as in backtest_chunks
    cohort       text NOT NULL CHECK (cohort IN ('short', 'long')),
    capital      double precision NOT NULL,
    promoted_on  date NOT NULL,                 -- marks start the day after
    entry        jsonb NOT NULL,                -- the gate values it entered on
    state        text NOT NULL DEFAULT 'trial' CHECK (state IN ('trial', 'winning', 'retired')),
    retired_on   date,
    retired_why  text,
    UNIQUE (strategy, exchange, overlay)
);

CREATE TABLE shadow_marks (
    account  text NOT NULL REFERENCES shadow_accounts ON DELETE CASCADE,
    day      date NOT NULL,
    ret      double precision NOT NULL,
    equity   double precision NOT NULL,
    PRIMARY KEY (account, day)
);
