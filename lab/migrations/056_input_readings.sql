-- Readings of the inputs in lab/inputs.toml that a reader of ours fetches (plan recorder-links-vivid-stream, P5). One
-- writer per input, its `reader`; as_of is when we read it, so a backtest that shows a strategy only the readings
-- as_of at or before a row cannot see a forecast before it was made. key is the input's own unit (a station).
CREATE TABLE input_readings (
    input text NOT NULL,
    key   text NOT NULL,
    as_of timestamptz NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY (input, key, as_of)
);
CREATE INDEX input_readings_as_of ON input_readings (input, as_of);
