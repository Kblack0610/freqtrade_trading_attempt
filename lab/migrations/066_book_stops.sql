-- Per scored strategy and [book] stage: the share of its 12-month out-of-sample block-bootstrap paths whose fall from
-- peak reaches the stage's hard stop (a share of capital), rebuilt by lab/crypto/book.py beside `book`.
CREATE TABLE book_stops (
    strategy        text NOT NULL,
    exchange        text NOT NULL,
    stage           text NOT NULL,
    horizon_months  integer NOT NULL,
    hard_stop       double precision NOT NULL,
    p_hard_stop     double precision NOT NULL,
    basis_months    integer NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, stage, horizon_months)
);
