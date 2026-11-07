-- broker_balances now holds read-only reads of real accounts (Robinhood Crypto, Fidelity through SnapTrade) beside the
-- paper ones, so each row says which it is. The rows before this were all Alpaca paper.
ALTER TABLE broker_balances ADD COLUMN paper boolean NOT NULL DEFAULT true;
ALTER TABLE broker_balances ALTER COLUMN paper DROP DEFAULT;
