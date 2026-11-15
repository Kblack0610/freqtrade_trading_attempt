-- League bots' entries join the decision record (plan recorder-links-vivid-stream, P4): lab/ingest.py writes one
-- `crypto` decision per bot trade from its entry candle (lab/entry_inputs.py), keyed like bot_trades. A freqtrade
-- trade_id counts per bot and can repeat after a lost sqlite, so the key widens to account and decided_at (the
-- trade's open_date); a paper order's id is unique on its own, so its rows are unaffected.
ALTER TABLE decisions DROP CONSTRAINT decisions_fill_source_fill_id_key;
ALTER TABLE decisions ADD CONSTRAINT decisions_fill UNIQUE (fill_source, account, fill_id, decided_at);
