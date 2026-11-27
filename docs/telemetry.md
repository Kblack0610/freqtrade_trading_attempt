# Telemetry

The trader sends no end-user analytics: the site is read-only behind Forgejo sign-in and loads no tracker. Its events are the rows the lab records in its own database, so the catalogue lists those. `event-catalogue` (dotfiles) reads the table below through the `analytics` row of `departments.tsv`, `trader:docs/telemetry.md`; the heading and the Kind, When, Props columns are its format, so keep them.

## Usage events

| Kind | When | Props |
|---|---|---|
| `research_enters`, `research_leaves` | the nightly research loop (`lab/crypto/loop.py`) finds a strategy or mixture whose climate cell, or an open promote suggestion, entered or left the passing set: a `research_events` row, `change` is `enters` or `leaves` | `signal`, `kind`, `name`, `exchange`, `variant`, `cell`, `lo`, `annual`, `trials`, `action` |
| `call_rule_event` | an event-to-call rule (`lab/inputs/call_rules.py`) matched a feed event and made or joined a call: a `call_rule_events` row, append-only | `rule`, `variant`, `feed_source`, `feed_id`, `ticker`, `ingested_at`, `call_id` |
