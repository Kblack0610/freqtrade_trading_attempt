-- Failed scenarios remain evidence; old folds lack evidence and fail confidence until recomputed.
ALTER TABLE walkforward ADD COLUMN robustness jsonb;
