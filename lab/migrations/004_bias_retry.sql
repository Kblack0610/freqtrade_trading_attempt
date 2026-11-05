-- Bias checks before this ran without --recursive-strategy-search, so every
-- strategy in a subfolder failed to load. An errored check waits a week for
-- its retry; clearing them makes the next run check again.
DELETE FROM bias_checks WHERE has_bias IS NULL;
