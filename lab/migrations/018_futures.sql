-- Measured perpetual-swap data, one writer: lab/futures.py. Free history is short (okx funding about 3 months,
-- open interest 3 to 6 months), so collection runs daily and the tables are kept forever.

CREATE TABLE funding_rates (
    exchange  text NOT NULL,         -- the spot venue name, okx or kucoin
    pair      text NOT NULL,         -- the swap symbol, BTC/USDT:USDT
    ts        timestamptz NOT NULL,  -- the funding settlement time
    rate      double precision NOT NULL,
    PRIMARY KEY (exchange, pair, ts)
);

CREATE TABLE open_interest (
    exchange   text NOT NULL,
    pair       text NOT NULL,
    ts         timestamptz NOT NULL,  -- start of the day the reading closes
    contracts  double precision,      -- what kucoin reports
    value_usd  double precision,      -- what okx reports
    PRIMARY KEY (exchange, pair, ts)
);
