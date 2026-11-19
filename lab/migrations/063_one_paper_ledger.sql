-- One paper ledger (plan waths-a-better-tradign-enchanted-dongarra, P4): the stock league's accounts and positions
-- become paper_accounts (venue alpaca) and paper_orders rows, written by lab/stages/paper.py like every other market.
-- A position is one order: shares in contracts, the ticker in market_id, side long, the exit in settled_at, payout
-- (the exit price), exit_fee and exit_reason. A stock is entered again after it exits, so an order is unique per
-- account and market only while it is open.
DROP VIEW fills;
ALTER TABLE paper_orders DROP CONSTRAINT paper_orders_account_market_id_key;
ALTER TABLE paper_orders ALTER COLUMN contracts TYPE double precision;
ALTER TABLE paper_orders ADD COLUMN exit_fee double precision, ADD COLUMN exit_reason text, ADD COLUMN stock_id bigint;
CREATE UNIQUE INDEX paper_orders_one_open ON paper_orders (account, market_id) WHERE settled_at IS NULL;

INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, retired_at, equity, realized,
                            open_positions, marked_at)
SELECT account, family, 'alpaca', variant, params, capital, started_at, retired_at, equity, realized, open_positions,
       marked_at
FROM stock_accounts;

INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee, settled_at,
                          payout, exit_fee, profit, exit_reason, stock_id)
SELECT account, ticker, 'long', shares * entry_price, entered_at, quoted_at, shares, entry_price, entry_cost, exited_at,
       exit_price, exit_cost, profit, exit_reason, id
FROM stock_positions ORDER BY id;

-- The decisions keep their fill: the stock id becomes the paper order id.
UPDATE decisions d SET fill_source = 'paper', fill_id = o.id
FROM paper_orders o WHERE d.fill_source = 'stock' AND o.stock_id = d.fill_id;
ALTER TABLE paper_orders DROP COLUMN stock_id;
DROP TABLE stock_positions;
DROP TABLE stock_accounts;

CREATE VIEW fills AS
SELECT 'freqtrade'::text AS source, t.bot AS account, b.strategy, b.exchange AS venue, t.pair AS instrument,
       t.trade_id::bigint AS fill_id, t.open_date, t.close_date, t.is_open, t.stake_amount AS stake,
       t.profit_abs, t.profit_ratio, t.exit_reason
FROM bot_trades t JOIN bots b ON b.name = t.bot
UNION ALL
SELECT 'paper', o.account, a.family, a.venue, o.market_id, o.id, o.sent_at, o.settled_at, o.settled_at IS NULL,
       o.contracts * o.price + o.fee, o.profit, o.profit / nullif(o.contracts * o.price + o.fee, 0),
       CASE WHEN o.settled_at IS NOT NULL THEN coalesce(o.exit_reason, 'settled') END
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
FROM calls c LEFT JOIN call_scores s ON s.call_id = c.id AND s.horizon = (SELECT max(h) FROM unnest(c.horizons) h);
