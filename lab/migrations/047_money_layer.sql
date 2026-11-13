-- What traded, one row per fill on every engine, read-only: league bots (bot_trades), the event-contract paper league
-- (paper_orders that filled) and the order drills (demo_orders that executed). Each source keeps its one writer.
-- lab/confidence.py scores live trading from it, and its columns are the ones lab/metrics.py daily_returns reads.
-- A paper fill's stake is its cost with the entry fee; its profit is net of every fee. A drill fill has no strategy
-- and never settles in our records.
CREATE VIEW fills AS
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
WHERE d.status IN ('executed', 'filled');

-- One bankroll across every venue, one row, rewritten each pass by lab/watcher.py. cap is the [live] stage's capital;
-- capital and equity sum every money (not dry-run) watch row; peak is the highest summed profit (equity less capital)
-- seen, so an account joining or leaving moves the fall only by its own profit. breached_at holds until
-- a human clears it (lab/watcher.py --bankroll-clear), and while it holds every money account's entries stay stopped
-- and lab/guard.py refuses a start.
CREATE TABLE bankroll (
    id           boolean PRIMARY KEY DEFAULT true CHECK (id),
    checked_at   timestamptz NOT NULL,
    stage        text NOT NULL,
    cap          double precision NOT NULL,
    hard_stop    double precision NOT NULL,
    accounts     integer NOT NULL,
    capital      double precision NOT NULL,
    equity       double precision NOT NULL,
    peak         double precision NOT NULL,
    breached_at  timestamptz,
    reason       text
);
