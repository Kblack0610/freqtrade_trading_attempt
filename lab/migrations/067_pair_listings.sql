-- Which pairs a venue listed, from lab/crypto/listings.py: each USDT pair's window (its first and last 1h candle) and
-- the months it was listed in, so a rerun can use the universe of the time instead of today's survivors.
CREATE TABLE pair_windows (
    exchange    text NOT NULL,
    pair        text NOT NULL,
    status      text NOT NULL,
    first_ts    timestamptz NOT NULL,
    last_ts     timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, pair)
);

CREATE TABLE pair_listings (
    exchange    text NOT NULL,
    month       date NOT NULL,
    pair        text NOT NULL,
    PRIMARY KEY (exchange, month, pair)
);
