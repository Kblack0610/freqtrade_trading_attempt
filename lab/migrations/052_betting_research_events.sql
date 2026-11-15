-- The betting lane joins the nightly research loop (lab/loop.py): a variant entering or leaving the markets
-- readiness gate's ready set (bet-ready, cell '') and an unretired paper account whose forward check drifted from
-- its backtest (bet-drift, cell is the account). research_events.signal has no check, so only the state widens.
ALTER TABLE research_state DROP CONSTRAINT research_state_signal_check;
ALTER TABLE research_state ADD CONSTRAINT research_state_signal_check
    CHECK (signal IN ('climate', 'promote', 'retune', 'bet-ready', 'bet-drift'));
