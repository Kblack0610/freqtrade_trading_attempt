-- Cohorts (lab/league.py): each roster row names the league it reports in and the setup overlay it runs under
-- (lab/variants.toml id, '' for the strategy's own settings, as backtest_chunks.overlay). Ingest writes both from the
-- roster on every run, so a bot moved to another cohort reports there from its next snapshot.
ALTER TABLE bots ADD COLUMN cohort text NOT NULL DEFAULT 'league', ADD COLUMN overlay text NOT NULL DEFAULT '';
