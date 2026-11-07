-- The long-short trend research (lab/research/DonchianLongShort.py, docs/short-bust.md), counted in the trial
-- register though it stays out of the league, one row per basket as for BustShort (017). Rerunning BustShort with
-- measured funding is the same rule on the same baskets, so it adds no trial.
INSERT INTO trials (strategy, exchange, variant) VALUES ('DonchianLongShort', 'okx', 'majors'), ('DonchianLongShort', 'okx', 'ai')
ON CONFLICT DO NOTHING;
