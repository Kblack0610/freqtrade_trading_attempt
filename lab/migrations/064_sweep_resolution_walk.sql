-- Where each sweep left its resolution walk (lab/markets/recorder.py sweep()). A walk that runs past
-- [recorder] resolutions_budget_s stops at a page and the next sweep resumes it from walk_cursor with the same
-- walk_since; walk_started is the sweep that began the walk, and the next new walk starts from it. A walk that
-- reached walk_since leaves walk_cursor null. Null on rows before 064, which read from started_at as before.
ALTER TABLE market_sweeps
    ADD COLUMN walk_since   timestamptz,
    ADD COLUMN walk_cursor  text,
    ADD COLUMN walk_started timestamptz;
