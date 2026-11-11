-- Where a fold's time goes and how it tuned: hyperopt and backtest seconds apart, the sampler arm, the search
-- spaces and the epochs hyperopt ran before early stop. NULL on folds computed before.
ALTER TABLE walkforward ADD COLUMN hyperopt_s double precision, ADD COLUMN backtest_s double precision,
    ADD COLUMN sampler text, ADD COLUMN spaces text, ADD COLUMN epochs_run integer;
