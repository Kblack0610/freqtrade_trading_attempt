-- History backfill, one writer: lab/markets/history.py. Bars and trades go to parquet in MinIO, never here; this
-- holds only how far each walk and each settle date has got, so a killed run resumes and a rerun redoes nothing.

-- Contracts traded over a market's life, as the venue states it at settlement: what the backfill's volume floors read.
ALTER TABLE resolutions ADD COLUMN volume double precision;

CREATE TABLE market_backfill (
    venue       text NOT NULL,
    job         text NOT NULL,            -- settled, bars, trades
    unit        text NOT NULL,            -- '' for the settled walk; the UTC settle date for bars and trades
    cursor      text,                     -- settled: where the walk resumes; null once it is over
    markets     integer NOT NULL DEFAULT 0, -- bars and trades: the markets the date had when it was done
    rows        bigint NOT NULL DEFAULT 0,
    object_key  text,
    done        boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (venue, job, unit)
);
