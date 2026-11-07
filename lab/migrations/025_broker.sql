-- Broker account balances, one writer: lab/broker.py. One row per broker per read, paper accounts only.
CREATE TABLE broker_balances (
    broker        text NOT NULL,                  -- alpaca_paper
    read_at       timestamptz NOT NULL,
    status        text,                           -- the broker's word for the account, ACTIVE; null when the read failed
    currency      text,
    equity        double precision,
    cash          double precision,
    buying_power  double precision,
    positions     integer,
    error         text,
    PRIMARY KEY (broker, read_at)
);
