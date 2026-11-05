-- What a scored strategy might return, rebuilt by lab/book.py after every score. basis is 'strategy' for its
-- out-of-sample monthly returns or 'hold' for BTC over the same months; basis_months is how many months fed it.
CREATE TABLE book (
    strategy          text NOT NULL,
    exchange          text NOT NULL,
    basis             text NOT NULL,
    horizon_months    integer NOT NULL,
    p5                double precision NOT NULL,
    p50               double precision NOT NULL,
    p95               double precision NOT NULL,
    prob_loss         double precision NOT NULL,
    exp_max_drawdown  double precision NOT NULL,
    paths             integer NOT NULL,
    basis_months      integer NOT NULL,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, exchange, basis, horizon_months)
);
