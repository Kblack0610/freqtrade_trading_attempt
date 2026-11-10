-- Causal daily HAR forecasts and whether prior months already beat both baselines, for research sizing only.
CREATE TABLE vol_forecasts (
    exchange text NOT NULL,
    pair text NOT NULL,
    day date NOT NULL,
    variance double precision NOT NULL CHECK (variance > 0),
    eligible boolean NOT NULL,
    PRIMARY KEY (exchange, pair, day)
);
ALTER TABLE walkforward_trades DROP CONSTRAINT walkforward_trades_variant_check;
ALTER TABLE walkforward_trades ADD CHECK (variant IN ('base', 'tuned', 'har_target'));
