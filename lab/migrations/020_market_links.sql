-- Same-event links across venues, one writer: lab/markets/links.py. A link says outcome_a of market a pays exactly
-- when outcome_b of market b pays. An outcome word is the market's outcome without any quoter prefix: yes for a
-- binary market, the team for an odds_api game (whose stored outcomes are <book>:<team>).
-- A link counts only once confirmed: by a rule when it is made, or by both markets resolving the same way.
-- A rule link whose markets resolve apart is rejected too, so a broken rule shows up here (plan
-- id-like-to-setup-abundant-seahorse, Risks: event-matching errors would create fake arbitrage).

CREATE TABLE market_links (
    id               bigserial PRIMARY KEY,
    venue_a          text NOT NULL,
    market_id_a      text NOT NULL,
    outcome_a        text NOT NULL,
    venue_b          text NOT NULL,
    market_id_b      text NOT NULL,
    outcome_b        text NOT NULL,
    source           text NOT NULL,           -- rule:<name> or llm:<model>
    score            double precision,        -- the LLM's stated confidence, null for a rule
    reason           text,
    status           text NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    confirmed_by     text CHECK (confirmed_by IN ('rule', 'resolution')),
    resolved_agrees  boolean,                 -- null until both markets resolve and neither is void
    proposed_at      timestamptz NOT NULL DEFAULT now(),
    checked_at       timestamptz,             -- when the resolution check last settled this link
    UNIQUE (venue_a, market_id_a, outcome_a, venue_b, market_id_b, outcome_b),
    FOREIGN KEY (venue_a, market_id_a) REFERENCES markets,
    FOREIGN KEY (venue_b, market_id_b) REFERENCES markets
);
CREATE INDEX market_links_b ON market_links (venue_b, market_id_b);
CREATE INDEX market_links_unchecked ON market_links (id) WHERE checked_at IS NULL;

-- One row per market the LLM was asked about, so an unmatched market is not paid for again every run.
CREATE TABLE market_link_asks (
    venue_a      text NOT NULL,
    market_id_a  text NOT NULL,
    venue_b      text NOT NULL,
    model        text NOT NULL,
    proposed     integer NOT NULL,            -- links it proposed, 0 for none
    asked_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (venue_a, market_id_a, venue_b),
    FOREIGN KEY (venue_a, market_id_a) REFERENCES markets
);
