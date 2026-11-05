-- Rule-based suggestions (lab/suggest.py, thresholds in lab/suggest.toml), rebuilt after every analysis.
-- exchange is '' for a suggestion about the strategy file itself (retire, fix). first_seen survives
-- rebuilds, so the site can say how long a suggestion has held; a row that no longer applies is deleted.
CREATE TABLE suggestions (
    kind        text NOT NULL,
    strategy    text NOT NULL,
    exchange    text NOT NULL,
    bot         text NOT NULL DEFAULT '',
    score       double precision,
    summary     text NOT NULL,
    action      text NOT NULL,
    gates       jsonb NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, strategy, exchange, bot)
);
