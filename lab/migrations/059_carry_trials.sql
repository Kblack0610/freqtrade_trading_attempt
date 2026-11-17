-- The carry research (lab/research/carry.py, docs/carry.md), counted in the trial register though nothing here
-- trades: one row per rule measured on kucoin funding and spot candles, as carry.TRIALS names them.
INSERT INTO trials (strategy, exchange, variant) VALUES
    ('CarryHarvest', 'kucoin', 'always'),
    ('CarryHarvest', 'kucoin', 'smoothed'),
    ('CarryHarvest', 'kucoin', 'last-day'),
    ('CarryHarvest', 'kucoin', 'top-third'),
    ('CarrySpot', 'kucoin', 'trend'),
    ('CarrySpot', 'kucoin', 'trend-low-funding'),
    ('CarrySpot', 'kucoin', 'low-funding-third'),
    ('CarryMix', 'kucoin', '60-40'),
    ('FundingForecast', 'kucoin', 'level'),
    ('FundingForecast', 'kucoin', 'own-history')
ON CONFLICT DO NOTHING;
