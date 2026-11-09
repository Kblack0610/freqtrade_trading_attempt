-- The nightly research loop (lab/loop.py), run at the end of each analysis.
-- research_state is what passed on the last run: a strategy or mixture whose climate_scorecard cell has its low end
-- above zero (signal climate), and each open promote suggestion (signal promote, cell ''). research_events is the
-- change against that set: one row per entry that enters or leaves it. reported_at is set by ci/research_sync.py
-- once the change is on the agent board (and, for a promotion, a roster PR is open), so nothing is filed twice.
CREATE TABLE research_state (
    signal    text NOT NULL CHECK (signal IN ('climate', 'promote')),
    kind      text NOT NULL,              -- strategy or mixture; '' for a promotion
    name      text NOT NULL,
    exchange  text NOT NULL,
    variant   text NOT NULL,
    cell      text NOT NULL,              -- <trend>/<vol bucket>; '' for a promotion
    lo        double precision,
    annual    double precision,
    action    text NOT NULL DEFAULT '',
    PRIMARY KEY (signal, kind, name, exchange, variant, cell)
);

CREATE TABLE research_events (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    signal       text NOT NULL,
    change       text NOT NULL CHECK (change IN ('enters', 'leaves')),
    kind         text NOT NULL,
    name         text NOT NULL,
    exchange     text NOT NULL,
    variant      text NOT NULL,
    cell         text NOT NULL,
    lo           double precision,
    annual       double precision,
    trials       integer NOT NULL,          -- the trial register's size when it changed: the bar behind the interval
    action       text NOT NULL DEFAULT '',  -- a promotion's roster line
    reported_at  timestamptz
);
CREATE INDEX research_events_unreported ON research_events (id) WHERE reported_at IS NULL;
