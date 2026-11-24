-- An entry `[paper] max_open` turned away is kept as a `capped` decision (lab/events/paper.py trade), with its edge
-- and rank in sizing, so a later cap change can be read against what the capped entries would have won.
ALTER TABLE decisions DROP CONSTRAINT decisions_action_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_action_check CHECK (action IN ('enter', 'exit', 'near_miss', 'capped'));
