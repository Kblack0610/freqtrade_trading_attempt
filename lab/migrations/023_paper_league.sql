-- The event-contract paper league, one writer: lab/markets/paper.py. One account per family variant, each with its
-- own paper capital; every order a strategy sends is one paper_orders row, filled or not (contracts 0), so a market
-- is entered once per account the way a backtest enters it. lab/watcher.py reads paper_accounts for the daily loss
-- watch and writes the kill drill into watch, which the next paper pass obeys.
CREATE TABLE paper_accounts (
    account         text PRIMARY KEY,          -- paper-<family>-<variant[:8]>, the watch row's bot
    family          text NOT NULL,
    venue           text NOT NULL,
    variant         text NOT NULL,             -- md5 of params, as in trials
    params          jsonb NOT NULL,
    capital         double precision NOT NULL,
    started_at      timestamptz NOT NULL,
    equity          double precision,          -- capital + realized + open positions marked at their exit price
    realized        double precision,
    open_positions  integer,
    marked_at       timestamptz                -- the last pass; the watcher's seen_at for this account
);

CREATE TABLE paper_orders (
    id           bigserial PRIMARY KEY,
    account      text NOT NULL REFERENCES paper_accounts,
    market_id    text NOT NULL,
    side         text NOT NULL,                -- yes or no
    stake        double precision NOT NULL,
    max_price    double precision,             -- the order's limit, null for none
    sent_at      timestamptz NOT NULL,
    quoted_at    timestamptz NOT NULL,         -- the recorded book it met
    bid          double precision,
    ask          double precision,
    contracts    integer NOT NULL,             -- 0 when nothing filled
    price        double precision,
    fee          double precision NOT NULL,
    settled_at   timestamptz,
    payout       double precision,             -- per contract
    profit       double precision,             -- net of every fee
    UNIQUE (account, market_id)
);
CREATE INDEX paper_orders_open ON paper_orders (account) WHERE settled_at IS NULL AND contracts > 0;

-- Orders on the Kalshi demo exchange, one writer: lab/markets/kalshi_trade.py. Mock funds only: the client refuses
-- any host but the demo's.
CREATE TABLE demo_orders (
    id           bigserial PRIMARY KEY,
    venue        text NOT NULL,
    ticker       text NOT NULL,
    client_id    text NOT NULL UNIQUE,
    order_id     text,
    price        double precision NOT NULL,
    count        integer NOT NULL,
    placed_at    timestamptz NOT NULL,
    status       text,                         -- the venue's word after the last read: resting, canceled, executed
    canceled_at  timestamptz,
    error        text
);
