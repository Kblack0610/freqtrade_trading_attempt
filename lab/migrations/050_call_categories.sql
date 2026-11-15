-- Classify the Phase 1 seed from its pre-registered catalyst. Any other legacy call needs an explicit
-- classification before this migration can finish; do not silently label unknown calls as events.
ALTER TABLE calls ADD COLUMN signal_source text;
ALTER TABLE calls DISABLE TRIGGER calls_append_only;
UPDATE calls SET signal_source = 'event'
WHERE source = 'plan easy-trade-to-start-cozy-russell (2026-11-13), Phase 1 seed call';
ALTER TABLE calls ENABLE TRIGGER calls_append_only;
ALTER TABLE calls ALTER COLUMN signal_source SET NOT NULL;
ALTER TABLE calls ADD CONSTRAINT calls_signal_source_present CHECK (btrim(signal_source) <> '');
