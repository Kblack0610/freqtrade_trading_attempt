-- Calls: every trade idea, recorded before its outcome and scored after it (lab/calls.py). A human's or an agent's
-- call is written by `python -m lab.calls add`, which refuses an entry close that has already printed; a rule's by
-- the rule runner. Rows are append-only: a wrong call is answered by a new one, never edited, so the record cannot
-- be flattered after the fact. created_at is the server's clock.
CREATE TABLE calls (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    who            text NOT NULL CHECK (who IN ('human', 'agent', 'rule')),
    author         text NOT NULL,                  -- the person, session or rule name
    thesis         text NOT NULL,
    ticker         text NOT NULL,
    direction      text NOT NULL CHECK (direction IN ('long', 'short')),
    instrument     text NOT NULL CHECK (instrument IN ('equity', 'etf')),  -- what lab/calls.py can price today
    entry_session  date NOT NULL,                  -- the call enters at this session's regular close
    horizons       integer[] NOT NULL DEFAULT '{1,5,20}' CHECK (cardinality(horizons) > 0 AND 0 < ALL (horizons)),
    benchmark      text NOT NULL,
    source         text                            -- a URL or the record the call cites
);

-- One row per call and horizon once its exit close has printed, written only by the nightly analysis (lab/calls.py
-- run). ret is the underlying's close-to-close return; pnl is the call's own return after borrow (a short) and the
-- per-side cost; excess is the direction times (ret less the benchmark's return over the same sessions).
CREATE TABLE call_scores (
    call_id       bigint NOT NULL REFERENCES calls (id),
    horizon       integer NOT NULL,
    exit_session  date NOT NULL,
    entry_close   double precision NOT NULL,
    exit_close    double precision NOT NULL,
    bench_entry   double precision NOT NULL,
    bench_exit    double precision NOT NULL,
    ret           double precision NOT NULL,
    pnl           double precision NOT NULL,
    excess        double precision NOT NULL,
    scored_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (call_id, horizon)
);

CREATE FUNCTION refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP;
END $$;
CREATE TRIGGER calls_append_only BEFORE UPDATE OR DELETE ON calls FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER call_scores_append_only BEFORE UPDATE OR DELETE ON call_scores
    FOR EACH ROW EXECUTE FUNCTION refuse_change();

-- The fills view gains calls as one more source: one row per call, closed at its longest horizon's score, a stake
-- of one unit of notional so profit_abs and profit_ratio are both the call's net return. lab/confidence.py reads
-- strategies' live fills and leaves calls out, the way it leaves drills out.
CREATE OR REPLACE VIEW fills AS
SELECT 'freqtrade'::text AS source, t.bot AS account, b.strategy, b.exchange AS venue, t.pair AS instrument,
       t.trade_id::bigint AS fill_id, t.open_date, t.close_date, t.is_open, t.stake_amount AS stake,
       t.profit_abs, t.profit_ratio, t.exit_reason
FROM bot_trades t JOIN bots b ON b.name = t.bot
UNION ALL
SELECT 'paper', o.account, a.family, a.venue, o.market_id, o.id, o.sent_at, o.settled_at, o.settled_at IS NULL,
       o.contracts * o.price + o.fee, o.profit, o.profit / nullif(o.contracts * o.price + o.fee, 0),
       CASE WHEN o.settled_at IS NOT NULL THEN 'settled' END
FROM paper_orders o JOIN paper_accounts a USING (account)
WHERE o.contracts > 0
UNION ALL
SELECT 'drill', d.venue, NULL, d.venue, d.ticker, d.id, d.placed_at, NULL, true, d.price * d.count, NULL, NULL, NULL
FROM demo_orders d
WHERE d.status IN ('executed', 'filled')
UNION ALL
SELECT 'call', c.who, NULL, 'call', c.ticker, c.id, (c.entry_session + time '16:00') AT TIME ZONE 'America/New_York',
       (s.exit_session + time '16:00') AT TIME ZONE 'America/New_York', s.call_id IS NULL, 1.0, s.pnl, s.pnl,
       CASE WHEN s.call_id IS NOT NULL THEN 'horizon' END
FROM calls c LEFT JOIN call_scores s ON s.call_id = c.id AND s.horizon = (SELECT max(h) FROM unnest(c.horizons) h);

-- The seed call, registered in ~/.claude/plans/easy-trade-to-start-cozy-russell.md (the file's birth time is
-- created_at). Its entry close had printed 95 minutes before; every horizon it is scored at was still ahead. Calls
-- made since go through `lab.calls add`, which refuses an entry close that has printed.
INSERT INTO calls (created_at, who, author, thesis, ticker, direction, instrument, entry_session, horizons, benchmark,
                   source)
VALUES ('2026-11-13T14:35:23-08:00', 'human', 'kenneth',
        'The board ousted Huang on 2026-11-12; the Nov 18 testimony and all-hands are further downside catalysts.',
        'NVDA', 'short', 'equity', '2026-11-13', '{1,5,20}', 'SMH',
        'plan easy-trade-to-start-cozy-russell (2026-11-13), Phase 1 seed call');
