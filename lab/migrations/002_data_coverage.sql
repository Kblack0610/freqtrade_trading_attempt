-- What price history the backfill job holds, one row per exchange x pair x timeframe.

CREATE TABLE data_coverage (
    exchange         text NOT NULL,
    pair             text NOT NULL,
    timeframe        text NOT NULL,
    first_ts         timestamptz,
    last_ts          timestamptz,
    rows             bigint NOT NULL DEFAULT 0,
    gaps             integer NOT NULL DEFAULT 0,  -- runs of missing candles
    missing_candles  bigint NOT NULL DEFAULT 0,
    -- true once a prepend pass left first_ts where it was: the exchange has nothing older
    oldest_reached   boolean NOT NULL DEFAULT false,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, pair, timeframe)
);
