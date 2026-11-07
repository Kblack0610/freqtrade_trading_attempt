-- Watch-only self-custody balances, one writer: lab/wallet.py. One row per account per read; the public key
-- itself is never stored, only the account's name from WALLET_ACCOUNTS.
CREATE TABLE wallet_balances (
    account          text NOT NULL,
    read_at          timestamptz NOT NULL,
    coin             text NOT NULL,                -- btc
    confirmed_sat    bigint,                       -- null when the read failed
    unconfirmed_sat  bigint,                       -- mempool delta, can be negative while a send is unconfirmed
    used_addresses   integer,
    error            text,
    PRIMARY KEY (account, read_at)
);
