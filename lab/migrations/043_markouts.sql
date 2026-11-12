-- Signal markouts (lab/markouts.py): each paper entry's move at fixed horizons after its fill, hedged by the pair's
-- BTC beta and net of the venue's round-trip fee, beside the same figure for random entries on the same pair and side
-- (the null). Written once per entry and horizon, when the candles cover the entry plus the null window.
CREATE TABLE markouts (
    bot          text NOT NULL REFERENCES bots(name) ON DELETE CASCADE,
    trade_id     integer NOT NULL,
    open_date    timestamptz NOT NULL,
    horizon_min  integer NOT NULL,
    pair         text NOT NULL,
    exchange     text NOT NULL,
    side         smallint NOT NULL,          -- 1 long, -1 short
    fill_slip    double precision,           -- fill over the entry candle's open, signed by side, positive when worse
    ret          double precision,           -- the move from the fill to the horizon, signed by side
    btc_ret      double precision,           -- BTC's move over the same candles, unsigned
    beta         double precision,           -- the pair's BTC beta over [markouts] beta_days before the entry
    net          double precision,           -- ret minus side x beta x btc_ret minus the round-trip fee
    null_net     double precision,           -- the mean net of the random entries
    null_draws   integer NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot, trade_id, open_date, horizon_min)
);

-- Per trade reconciliation of a bot's dry run against the drift backtest of the same days (metrics.reconcile),
-- rewritten with the bot's drift row. kind: matched, live_only (the bot entered, the backtest did not) or bt_only
-- (a missed fill). The gaps are live minus backtest: delay in seconds, entry price as a ratio (positive when worse
-- for the side), round-trip fee rate, and profit ratio where both closed.
CREATE TABLE reconcile (
    bot         text NOT NULL REFERENCES bots(name) ON DELETE CASCADE,
    n           integer NOT NULL,
    pair        text NOT NULL,
    kind        text NOT NULL,
    live_open   timestamptz,
    bt_open     timestamptz,
    delay_s     double precision,
    price_gap   double precision,
    fee_gap     double precision,
    profit_gap  double precision,
    PRIMARY KEY (bot, n)
);
