-- Each window against holding BTC. capital_exposure is the mean share of the wallet in positions,
-- which `exposure` (any position open) overstates when capital is split across trades. hold_excess
-- is the annual return over holding that share of BTC, with its 90% block-bootstrap interval.
ALTER TABLE strategy_windows
    ADD COLUMN capital_exposure double precision,
    ADD COLUMN hold_excess      double precision,
    ADD COLUMN hold_excess_lo   double precision,
    ADD COLUMN hold_excess_hi   double precision;
