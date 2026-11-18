-- trades_needed grows as (z sd / mean)^2 when the mean per-trade return is barely positive: 2026-11-18's analysis
-- computed 4,089,182,763 for one window, past integer's 2^31, and the whole derive step failed on it.
ALTER TABLE strategy_windows ALTER COLUMN trades_needed TYPE bigint;
ALTER TABLE overlay_windows ALTER COLUMN trades_needed TYPE bigint;
