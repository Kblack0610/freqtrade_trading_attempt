-- Event-contract markets, one writer: lab/markets/recorder.py. Postgres holds what a market is and its latest
-- quote; every quote change goes to parquet in MinIO (bucket in lab/markets.toml), never here (plan
-- id-like-to-setup-abundant-seahorse, Rejected: ticks in Postgres).

CREATE TABLE venues (
    venue       text PRIMARY KEY,         -- the adapter's name, kalshi
    title       text NOT NULL,
    kind        text NOT NULL,            -- exchange, sportsbook, play-money
    url         text,
    first_seen  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE markets (
    venue        text NOT NULL REFERENCES venues,
    market_id    text NOT NULL,           -- the venue's own id, a Kalshi ticker
    event_id     text,
    series_id    text,
    title        text NOT NULL,
    subtitle     text,
    market_type  text NOT NULL,           -- binary, scalar
    status       text NOT NULL,           -- the venue's word; settled once a resolution is stored
    open_time    timestamptz,
    close_time   timestamptz,
    first_seen   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    PRIMARY KEY (venue, market_id)
);
CREATE INDEX markets_event ON markets (venue, event_id);
CREATE INDEX markets_open ON markets (venue, close_time) WHERE status NOT IN ('settled', 'finalized');

-- One row per tradable outcome, carrying its latest quote: the state the recorder diffs a sweep against.
-- A binary market has one outcome, yes; its no side is the complement.
CREATE TABLE outcomes (
    venue          text NOT NULL,
    market_id      text NOT NULL,
    outcome        text NOT NULL,
    bid            double precision,
    ask            double precision,
    last           double precision,
    bid_size       double precision,
    ask_size       double precision,
    volume         double precision,
    open_interest  double precision,
    quoted_at      timestamptz NOT NULL,
    PRIMARY KEY (venue, market_id, outcome),
    FOREIGN KEY (venue, market_id) REFERENCES markets
);

CREATE TABLE resolutions (
    venue        text NOT NULL,
    market_id    text NOT NULL,
    result       text NOT NULL,           -- yes, no, a contract id, or void
    value        double precision,        -- what one yes contract paid, in dollars
    settled_at   timestamptz NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (venue, market_id)
);
CREATE INDEX resolutions_settled ON resolutions (venue, settled_at);

-- One row per recorder run per venue: what P8's staleness alert reads.
CREATE TABLE market_sweeps (
    id           bigserial PRIMARY KEY,
    venue        text NOT NULL,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL,
    seen         integer NOT NULL,        -- open outcomes in the sweep
    new_markets  integer NOT NULL,
    changes      integer NOT NULL,        -- quote rows written to parquet
    resolved     integer NOT NULL,
    object_key   text,                    -- the parquet object, null when nothing changed
    ok           boolean NOT NULL,
    error        text
);
CREATE INDEX market_sweeps_venue ON market_sweeps (venue, started_at DESC);
