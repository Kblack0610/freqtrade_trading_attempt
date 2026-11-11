-- The median time a window's trades were open, in hours: the holding-period axis of the diversity archive
-- (lab/diversity.py, docs/strategy-coverage.md). NULL until the window's strategy reruns.
ALTER TABLE strategy_windows ADD COLUMN median_hold_hours double precision;
ALTER TABLE overlay_windows ADD COLUMN median_hold_hours double precision;
