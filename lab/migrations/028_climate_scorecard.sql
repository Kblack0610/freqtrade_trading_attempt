-- Out-of-sample result per market climate (BTC trend up/down/flat x volatility tercile), from lab/scorecard.py.
-- kind is strategy or benchmark (mixture joins with the mixtures phase). annual is the mean daily return x 365 with a
-- block-bootstrap interval; days are the calendar days of the climate inside the strategy's walk-forward span.
CREATE TABLE climate_scorecard (
    kind         text NOT NULL,
    name         text NOT NULL,
    exchange     text NOT NULL,
    variant      text NOT NULL,
    cell         text NOT NULL,              -- <trend>/<vol bucket>, e.g. up/low
    days         integer NOT NULL,
    trades       integer NOT NULL,
    annual       double precision,
    lo           double precision,           -- null while the cell has fewer than [scorecard] min_days days
    hi           double precision,
    trials       integer NOT NULL,           -- the trial register's size when this ran: the bar behind the interval
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, name, exchange, variant, cell)
);

-- Event-contract backtest result per contract category (sports, politics, economics, other), per-trade net return.
CREATE TABLE market_category_results (
    family   text NOT NULL,
    venue    text NOT NULL,
    variant  text NOT NULL,
    category text NOT NULL,
    trades   integer NOT NULL,
    mean     double precision,
    lo       double precision,
    hi       double precision,
    PRIMARY KEY (family, venue, variant, category),
    FOREIGN KEY (family, venue, variant) REFERENCES market_backtests ON DELETE CASCADE
);
