-- The stock league (plan recorder-links-vivid-stream, P6): paper accounts on a small ETF universe, filled at Alpaca's
-- latest quote with no order sent anywhere (lab/stocks.py). One writer: the stock pass, which writes a `stocks`
-- decision in the same transaction as each entry and exit. One account per family variant of lab/stocks.toml.
CREATE TABLE stock_accounts (
    account         text PRIMARY KEY,          -- stock-<family>-<variant[:8]>, the watch row's bot
    family          text NOT NULL,
    variant         text NOT NULL,             -- md5 of params (lab/grid.py variant_of)
    params          jsonb NOT NULL,
    capital         double precision NOT NULL,
    started_at      timestamptz NOT NULL,
    retired_at      timestamptz,               -- its variant left lab/stocks.toml: no entries, exits still run
    equity          double precision,          -- capital + realized + open positions at the bid less their cost
    realized        double precision,
    open_positions  integer,
    marked_at       timestamptz                -- the last pass; the watcher's seen_at for this account
);

-- One row per position, entry to exit. A long buys at the ask and sells at the bid, each side paying calls.toml
-- cost_bps on its notional.
CREATE TABLE stock_positions (
    id           bigserial PRIMARY KEY,
    account      text NOT NULL REFERENCES stock_accounts,
    ticker       text NOT NULL,
    shares       double precision NOT NULL CHECK (shares > 0),
    entered_at   timestamptz NOT NULL,
    quoted_at    timestamptz NOT NULL,         -- the quote it met
    entry_price  double precision NOT NULL,
    entry_cost   double precision NOT NULL,
    exited_at    timestamptz,
    exit_price   double precision,
    exit_cost    double precision,
    profit       double precision,             -- shares x (exit - entry) less both costs
    exit_reason  text
);
CREATE UNIQUE INDEX stock_positions_one_open ON stock_positions (account, ticker) WHERE exited_at IS NULL;

-- The fills view gains the stock league as source `stock`.
CREATE OR REPLACE VIEW fills AS
SELECT 'freqtrade'::text AS source, t.bot AS account, b.strategy, b.exchange AS venue, t.pair AS instrument,
       t.trade_id::bigint AS fill_id, t.open_date, t.close_date, t.is_open, t.stake_amount AS stake,
       t.profit_abs, t.profit_ratio, t.exit_reason
FROM bot_trades t JOIN bots b ON b.name = t.bot
UNION ALL
SELECT 'paper', o.account, a.family, a.venue, o.market_id, o.id, o.sent_at, o.settled_at, o.settled_at IS NULL,
       o.contracts * o.price + o.fee, o.profit, o.profit / nullif(o.contracts * o.price + o.fee, 0),
       CASE WHEN o.settled_at IS NOT NULL THEN 'settled' END
FROM paper_orders o JOIN paper_accounts a USING (account)
WHERE o.contracts > 0
UNION ALL
SELECT 'drill', d.venue, NULL, d.venue, d.ticker, d.id, d.placed_at, NULL, true, d.price * d.count, NULL, NULL, NULL
FROM demo_orders d
WHERE d.status IN ('executed', 'filled')
UNION ALL
SELECT 'call', c.who, NULL, 'call', c.ticker, c.id, (c.entry_session + time '16:00') AT TIME ZONE 'America/New_York',
       (s.exit_session + time '16:00') AT TIME ZONE 'America/New_York', s.call_id IS NULL, 1.0, s.pnl, s.pnl,
       CASE WHEN s.call_id IS NOT NULL THEN 'horizon' END
FROM calls c LEFT JOIN call_scores s ON s.call_id = c.id AND s.horizon = (SELECT max(h) FROM unnest(c.horizons) h)
UNION ALL
SELECT 'stock', p.account, a.family, 'alpaca', p.ticker, p.id, p.entered_at, p.exited_at, p.exited_at IS NULL,
       p.shares * p.entry_price + p.entry_cost, p.profit,
       p.profit / nullif(p.shares * p.entry_price + p.entry_cost, 0), p.exit_reason
FROM stock_positions p JOIN stock_accounts a USING (account);
