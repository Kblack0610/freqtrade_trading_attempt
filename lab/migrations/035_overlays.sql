-- Setup overlays (lab/variants.toml, lab/variants.py): a backtest chunk is also keyed by the overlay it ran under,
-- '' for the strategy as it stands, so every reader of the as-is results filters on overlay = ''. An overlay's
-- params are kept by id, so a stored result names what it ran under after the toml changes.
CREATE TABLE overlays (
    id          text PRIMARY KEY,
    grp         text NOT NULL,
    params      jsonb NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE backtest_chunks ADD COLUMN overlay text NOT NULL DEFAULT '';
ALTER TABLE backtest_chunks DROP CONSTRAINT backtest_chunks_strategy_exchange_chunk_start_key,
    ADD CONSTRAINT backtest_chunks_strategy_exchange_overlay_chunk_start_key UNIQUE (strategy, exchange, overlay, chunk_start);

-- The windows of each strategy x venue x overlay, the columns of strategy_windows; dsr deflates across the as-is
-- series and every overlay series together. A migration adding a strategy_windows column adds it here too.
CREATE TABLE overlay_windows (LIKE strategy_windows INCLUDING DEFAULTS, overlay text NOT NULL,
    PRIMARY KEY (strategy, exchange, overlay, win));
