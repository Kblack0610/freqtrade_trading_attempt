-- okx open interest moves from okx's 1D day, which opens at 16:00 UTC (UTC+8 midnight), to its 1Dutc day, which
-- opens at 00:00 UTC like kucoin's (lab/futures.py OI_PARAMS). The next run refetches okx's last 100 UTC days, so a
-- 16:00 row inside that span would be a second reading of the same day: those go. Older 16:00 rows have no UTC
-- replacement and stay; their ts is still the start of the day the reading closes, on okx's UTC+8 day.
DELETE FROM open_interest
WHERE exchange = 'okx'
  AND ts >= date_trunc('day', now(), 'UTC') - interval '99 days'
  AND ts <> date_trunc('day', ts, 'UTC');
