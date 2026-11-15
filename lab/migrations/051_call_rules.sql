-- Event-to-call rules (lab/call_rules.py): each feed event a rule matched, and the call it made or joined. A rule makes
-- one call per ticker, direction and entry close; a second event on the same name before that close joins the first
-- call instead of stacking a correlated one. The rule and its params hash are the call's trial (trials row
-- 'rule:<name>', exchange 'calls', variant), registered before the rule makes its first call. ingested_at is
-- feed-ingest's server clock at insert, the time the event was knowable. Append-only, like calls.
CREATE TABLE call_rule_events (
    rule         text NOT NULL,
    variant      text NOT NULL,
    feed_source  text NOT NULL,
    feed_id      text NOT NULL,
    ticker       text NOT NULL,
    ingested_at  timestamptz NOT NULL,
    call_id      bigint NOT NULL REFERENCES calls (id),
    seen_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rule, variant, feed_source, feed_id, ticker)
);
CREATE INDEX call_rule_events_call ON call_rule_events (call_id);
CREATE TRIGGER call_rule_events_append_only BEFORE UPDATE OR DELETE ON call_rule_events
    FOR EACH ROW EXECUTE FUNCTION refuse_change();
