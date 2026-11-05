-- One row per bot, rewritten every cadence by lab/watcher.py. Keyed by roster name, not a foreign
-- key on bots: a new roster row is watched before its first hourly ingest. seen_at is the last good
-- read; day_open is the first equity read of the UTC day; breached_at holds for the rest of that day.
-- drill_at forces a breach (lab/watcher.py --drill) and is the only thing that stops entries so far.
CREATE TABLE watch (
    bot          text PRIMARY KEY,
    checked_at   timestamptz NOT NULL,
    seen_at      timestamptz NOT NULL,
    error        text,
    dry_run      boolean,
    state        text,
    capital      double precision,
    equity       double precision,
    day          date,
    day_open     double precision,
    breached_at  timestamptz,
    stopped_at   timestamptz,
    drill_at     timestamptz
);
