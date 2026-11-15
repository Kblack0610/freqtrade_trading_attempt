-- Why each paper trade happened (plan recorder-links-vivid-stream, P1). One writer per league: the event-contract
-- league's pass (lab/markets/paper.py trade) writes a decision in the same transaction as each order it stores, and
-- up to `[paper] near_misses` near-misses per account per pass (every check of the family passed but one).
-- inputs: [{name, value, source, as_of}]; checks: [{name, value, threshold, pass}], market_readiness.gates' shape;
-- sizing: stake, depth, depth_share, cash, contracts, price, fee. fill_source/fill_id key the `fills` view row.
CREATE TABLE decisions (
    id          bigserial PRIMARY KEY,
    league      text NOT NULL,                  -- event_contracts; crypto and stocks join later
    account     text NOT NULL,
    venue       text NOT NULL,
    instrument  text NOT NULL,                  -- market_id, pair or ticker
    decided_at  timestamptz NOT NULL,
    action      text NOT NULL CHECK (action IN ('enter', 'exit', 'near_miss')),
    rule        text NOT NULL,
    inputs      jsonb NOT NULL DEFAULT '[]',
    checks      jsonb NOT NULL DEFAULT '[]',
    sizing      jsonb,
    fill_source text,
    fill_id     bigint,
    UNIQUE (fill_source, fill_id)
);
CREATE INDEX decisions_account ON decisions (league, account, decided_at);

-- Every row of a pass's book, counted per account by where it stopped: the first check it failed, or for a row
-- that passed them all what became of its order (ordered, no_cash, max_open). A pass on a stale book is one
-- `stale_book` row per account.
CREATE TABLE decision_funnel (
    league  text NOT NULL,
    account text NOT NULL,
    venue   text NOT NULL,
    pass_at timestamptz NOT NULL,
    reason  text NOT NULL,
    n       integer NOT NULL,
    PRIMARY KEY (league, account, pass_at, reason)
);
CREATE INDEX decision_funnel_pass ON decision_funnel (pass_at);  -- the [paper] trace_days cut
