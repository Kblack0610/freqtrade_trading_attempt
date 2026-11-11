-- Each matched trade's live-minus-backtest gap in open order, for the anytime-valid drift gate (lab/confidence.py).
-- Rows from before this column read NULL until the next analysis run recomputes them.
ALTER TABLE drift ADD COLUMN gaps double precision[];
