-- Monthly walk-forward fits use the existing state-change outbox; the action digest identifies a fit.
ALTER TABLE research_state DROP CONSTRAINT research_state_signal_check;
ALTER TABLE research_state ADD CONSTRAINT research_state_signal_check
    CHECK (signal IN ('climate', 'promote', 'retune'));
