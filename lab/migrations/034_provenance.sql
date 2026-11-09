-- What produced each cached backtest chunk and walk-forward fold, so lab/reproduce.py and a reader can name the
-- build, configs, candles and seed behind a stored result. revision is the commit the image was built from (its
-- GHCR tag is sha-<first 7>, which names the digest home-config pinned); config_digest the two config files'
-- digests; candles the candle digest in the cache key; seed the fold's hyperopt random state. NULL on rows
-- stored before this migration.
ALTER TABLE backtest_chunks ADD COLUMN revision text, ADD COLUMN config_digest text, ADD COLUMN candles text;
ALTER TABLE walkforward ADD COLUMN revision text, ADD COLUMN config_digest text, ADD COLUMN candles text,
    ADD COLUMN seed bigint;
