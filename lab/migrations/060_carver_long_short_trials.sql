-- Carver's long-short trend research (lab/research/CarverLongShort.py, AFTS 6 and 8, docs/short-bust.md), counted in
-- the trial register though it stays out of the league, one row per basket as for BustShort (017) and
-- DonchianLongShort (032).
INSERT INTO trials (strategy, exchange, variant) VALUES
    ('CarverLongShort64', 'okx', 'majors'), ('CarverLongShort64', 'okx', 'ai'),
    ('CarverLongShort16', 'okx', 'majors'), ('CarverLongShort16', 'okx', 'ai')
ON CONFLICT DO NOTHING;
