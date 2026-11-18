-- One readiness table, one writer: lab/readiness.py store(). It replaces `confidence` (crypto, keyed strategy and
-- exchange) and `market_readiness` (event contracts, keyed family, venue and variant). market is 'crypto' or 'events';
-- venue is the exchange or the venue; strategy is the strategy name, or 'family/variant' for an event contract, whose
-- family, variant and params are kept beside it. trials, windows and the interval are 0 or NULL where a market has none.
CREATE TABLE readiness (
    market        text NOT NULL,
    venue         text NOT NULL,
    strategy      text NOT NULL,
    family        text,
    variant       text,
    params        jsonb,
    score         double precision,
    ready         boolean NOT NULL,
    passed        integer NOT NULL,
    windows       integer NOT NULL DEFAULT 0,
    trades        integer NOT NULL,
    expectancy    double precision,
    expectancy_lo double precision,
    expectancy_hi double precision,
    dsr           double precision,
    pbo           double precision,
    trials        integer NOT NULL DEFAULT 0,
    gates         jsonb NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market, venue, strategy)
);

INSERT INTO readiness (market, venue, strategy, score, ready, passed, trades, expectancy, gates, computed_at)
SELECT 'crypto', exchange, strategy, score, ready, passed, oos_trades, oos_expectancy, gates, computed_at FROM confidence;

INSERT INTO readiness (market, venue, strategy, family, variant, params, windows, trades, expectancy, expectancy_lo,
                       expectancy_hi, dsr, pbo, trials, ready, passed, gates, computed_at)
SELECT 'events', venue, family || '/' || variant, family, variant, params, windows, trades, expectancy, expectancy_lo,
       expectancy_hi, dsr, pbo, trials, ready, passed, gates, computed_at FROM market_readiness;

DROP TABLE confidence;
DROP TABLE market_readiness;
