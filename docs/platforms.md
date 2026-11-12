# Platforms: what can be automated, and how

One record per broker, exchange, wallet and market we use or might use. Each record says what an agent can automate and how: first the official API, then the agent machine (the mac-mini), and last what stays with the person. Nothing here trades money. `lab/league.py` refuses any config that is not dry-run, and `lab/live.py` keeps `GUARD_LIFTED = False`. Until real money is in, every platform is paper or read-only. Plan: `plan-mode-a-thorough-hashed-pike` (tr-72). Real-money gates, stages and caps belong to the calm-lagoon plan (tr-9, tr-21).

Checked 2026-11-06. Confidence follows [markets.md](markets.md): **doc** means read on the platform's own page, **secondary** means a vendor, review or community page, and **snippet** means a search summary only. Anything with no source says **Not verified**. Re-check a snippet or Not-verified claim before code or money relies on it.

This page owns account, sign-in, terms, automation tier and cost. Data-API facts for prediction markets stay in [markets.md](markets.md). Crypto keys, the live checks and reconciliation stay in [live.md](live.md). Research venue fees stay in the README [Venues](../README.md#venues) table.

## Summary

Tier is the highest rung we can use: **API** (an official API covers the job), **machine** (the agent machine may drive the site or app), **person** (only the person may act). Paper is how we practise before money.

| Platform | Status | Group | Tier for reads | Tier for orders | Paper path | Next |
|---|---|---|---|---|---|---|
| Kraken | held | crypto exchange | API | API | dry-run league | P2, trade path tr-21 |
| Binance.US | used before | crypto exchange | API | API | dry-run league | P2 |
| Coinbase | candidate | crypto exchange | API | API, manual only | dry-run league | P2 |
| Gemini | candidate | crypto exchange | API | API | Gemini sandbox | P2 |
| Trezor | held | self-custody | API (xpub) | person (press on device) | none needed | P3 |
| Alpaca | candidate | API broker | API | API | Alpaca paper account | live 2026-11-12: hourly read and weekday drill (home-config #832) |
| Interactive Brokers | candidate | API broker | API | API | IBKR paper account, needs a funded account | when a strategy needs it |
| Schwab | candidate | API broker | API | API | none found | not connected: no paper API |
| Tastytrade | candidate | API broker | API | API | cert sandbox | when a strategy needs it |
| Fidelity | held | broker, no API | aggregator (SnapTrade, read connection) | person | SnapTrade sandbox for the read path | key deployed (home-config #832), waits on the person connecting Fidelity in SnapTrade |
| Robinhood | candidate | broker | API (crypto only) | API (crypto), person (rest) | none | code landed, needs a read-only key |
| Kalshi | candidate (tr-50) | prediction market | API | API | demo exchange | demo drill live 2026-11-12 (home-config #832); live key held, not deployed; live tr-48 |
| Polymarket US | candidate | prediction market | API | API | backtest engine only | none until an account is opened |
| Polymarket global | data only | prediction market | API | never (US is close-only) | n/a | none |
| PredictIt | data only | prediction market | API (one feed) | person | none | none |
| Manifold | data, order drill built | play money | API | API, bots allowed | the venue is play money | key (tr-118) |
| DraftKings, FanDuel | candidate | sportsbook | Odds API | person | backtest engine only | none: odds recorded every 6 h since 2026-11-12 |
| ProphetX, Novig | candidate | exchange-style sports venue (CFTC) | API | API (ProphetX terms allow bots) | ProphetX sandbox | when a strategy needs it |

## Shared rails

Every record below links here instead of repeating it.

**API first.** Use the platform's official API wherever it covers the job. API keys get least privilege:
- Read-only first.
- Trade only when a live stage needs it.
- Withdraw never.
- IP-allowlisted where the platform offers it.

Pod keys live in SOPS in home-config, as in [live.md](live.md). The person creates and rotates every key. No agent does.

**The agent machine.** The mac-mini drives a site only when the platform's terms allow it. Each record's "Agent machine" line says whether they do. Use it for what the API cannot do, such as a forced browser re-login. The API stays the default. The pattern is the `agent-machine` skill:
- `claude-mcp stores-browser` attaches the person's signed-in Chrome. Never use a Playwright-launched browser, and never the person's everyday profile.
- `agent-desktop` drives native apps. `see` first, act by label, then `see` again.
- Passwords: `rbw get <entry> | ssh -T mac-mini 'read -r v; ...'`. The secret never lands in a file, a logged command line or a kept screenshot.
- 2FA codes: `rbw code --folder trading <platform> | ssh -T mac-mini 'read -r v; ...'`. `rbw code` (alias `totp`) prints the current code for an entry that holds a TOTP secret. Checked against the installed rbw on 2026-11-06.
- Trading entries live in the rbw folder `trading`, named after the platform. The person adds them with `rbw add`. An agent never does.
- Screenshots that show balances or holdings go to `~/.cache/agent-machine/trading/` and are deleted when the session ends.
- Quit Chrome after the session.

**The person, always.**
- Opening or funding an account.
- Creating, rotating or downloading a key.
- `rbw add`, and `sops` edits.
- Any real-money order until a live stage allows the bot to place it.
- Legal attestations.
- Every press on the Trezor.

**Paper before money.** A platform reaches money only through `lab/live.py` and the calm-lagoon stages. Before that it is a dry-run bot, a demo or sandbox account, play money, or our own backtest (`lab/markets/backtest.py`).

**Kill switches.** Ours is `lab/watcher.py`: the daily-loss watch, which POSTs `/stopentry` to a Freqtrade bot. Live stages add the hard stop. Each platform's own switch, for the watcher to call once it trades there:

| Platform | Native switch | Confidence |
|---|---|---|
| Kraken | `POST /0/private/CancelAllOrdersAfter` dead-man timer: re-arm every 15 to 30 s with a 60 s timeout, `timeout` 0 disables it, max 86399. Needs the Create & Modify Orders or Cancel/Close Orders permission. Futures has `/cancelallordersafter` | doc (spot) |
| Gemini | `require-heartbeat` flag on the key: a missed heartbeat cancels open orders. Timeout and how the flag is set: Not verified. Also Cancel All Active Orders and Cancel All Session Orders (POST) | doc (heartbeat), listing only (cancel-all) |
| Alpaca | `DELETE /v2/orders` cancels all open orders. `DELETE /v2/positions?cancel_orders=true` cancels them and closes every position at market. Both answer 207 with a status per order or position, so the watcher checks each entry. No dead-man switch found | doc |
| Kalshi | Order groups auto-cancel when a 15 s fill cap is hit. Self-exclusion and funding caps cannot be lifted early | doc |
| Polymarket US | `POST /v1/orders/open/cancel` cancels every open order, optionally by market slug. A standalone cancel is never rejected by the 5 s latency stopgap. No dead-man switch found | doc |
| Manifold | `expiresMillisAfter` on each limit bet: Manifold cancels it on its own. `POST /v0/bet/cancel/{id}` for one bet. No cancel-all | doc |
| Polymarket global | `DELETE /cancel-all` | doc |
| Binance.US | No dead-man switch and no account-wide cancel-all found: `DELETE /api/v3/openOrders` needs a `symbol`, so the watcher loops over the pairs. Last resort: delete the key | doc (first 200k chars of the page) |
| Coinbase | No dead-man switch on Spot. `POST /orders/batch_cancel` cancels listed order ids. Cancel-on-disconnect exists only on Global Derivatives | doc |
| IBKR | `reqGlobalCancel` cancels all open orders, including those placed in TWS. Precautionary size and value limits in TWS Presets. No account-wide kill switch found | doc (cancel), snippet (limits) |
| Tastytrade | No cancel-all found: `DELETE /accounts/{n}/orders/{id}` per order, so the watcher loops. `GET /accounts/{n}/trading-status` reads the account's state | doc (API index) |
| Schwab | Not verified | |

**2FA as a decision.** A TOTP secret in rbw lets the agent machine pass 2FA unattended. On a money account that widens what an agent can do. The person decides per account whether to store the secret, or to approve each 2FA prompt instead.

## Crypto exchanges

Freqtrade's official spot list includes Kraken, OKX, Kucoin and others. Binance.US runs as exchange id `binanceus`. Coinbase and Gemini are not on the list ([Freqtrade exchanges](https://www.freqtrade.io/en/stable/exchanges/), doc).

### Kraken

- Status: held.
- Strategy fit: The likely first live venue for the league, because it has the deep book: calm-lagoon measured $384M of BTC 24h volume against $3.6M on Binance.US. Kraken Derivatives US perps, launched 2026-06-15 (snippet), are a possible US route for [short-bust.md](short-bust.md).
- API: REST and WebSocket.
  - Key permissions are granular, and Withdraw Funds is its own toggle (doc).
  - Rate limits count calls per key: Starter max 15, decaying 0.33/s; Intermediate 20 at 0.5/s; Pro 20 at 1/s. Ledger and history calls cost 2. Orders have a separate limiter (doc).
  - WebSocket tokens last 15 minutes (doc).
  - Freqtrade supports it, with stoploss on exchange (doc).
  - The API serves only 720 candles; we read history from the OHLCVT archive (README).
  - Spot sandbox: Not verified.
- Agent machine: Not needed for trading. ToS on bots and site automation: Not verified.
- The person: creates the read key now and a trade key at Stage 1; funds the account.
- Sign-in and 2FA: 2FA can also be set on an API key itself (snippet). Sign-in methods: Not verified.
- Risk controls: IP allowlist (doc), `CancelAllOrdersAfter`, key expiry and subaccounts (snippet). Ours: the `lab/live.py` checks, including stoploss on exchange.
- Cost: no API fee found. Perps charge 0.25% of notional to open and to close (doc, product unclear). Spot fee tiers: Not verified here; calm-lagoon quotes a 0.8 to 1.6% round trip.
- Paper: the dry-run league already runs Kraken bots.
- Read path: `KrakenRead` in `lab/broker.py`. `python -m lab.broker read` writes one `broker_balances` row named `kraken`: cash is the ZUSD balance, equity is `TradeBalance` `eb` (Kraken's own USD value of the whole account), positions the other assets held. Every private call is a POST, so the client sends only to `/0/private/Balance` and `/0/private/TradeBalance` and refuses any other path before sending; `lab/tests/test_money.py` pins those two. The body is JSON, and the nonce is in nanoseconds because the `kraken-read` key had already seen nanosecond nonces and Kraken refuses any lower one (measured 2026-11-12). Keys: `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` in the `trader-brokers` Secret.
- Verified: 2026-11-06. Sources: https://docs.kraken.com/exchange/guides/rest/api-keys, https://docs.kraken.com/api/docs/guides/spot-rest-ratelimits, https://www.kraken.com/features/fee-schedule, https://blog.kraken.com/product/kraken-derivatives/announcing-cftc-regulated-us-perps.

### Binance.US

- Status: used before.
- Strategy fit: lowest fees, but a thin book. Backtests assume fills its books may not give (README Venues).
- API:
  - HMAC-SHA256 with the `X-MBX-APIKEY` header; Ed25519 and RSA keys also work (doc).
  - Limits are per IP: request weight 6,000/min, raw requests 61,000 per 5 min. Overrunning brings a 418 ban of 2 min to 3 days (doc).
  - KYC comes before any key.
  - No testnet (doc). A separate withdraw permission: Not verified.
- Agent machine: no. The terms bar any "robot, spider, other automatic device" used on the website without written permission (snippet). The same terms acknowledge third-party apps and bots with account access, so API trading is the sanctioned route.
- The person: KYC, keys, funding.
- Sign-in and 2FA: Not verified.
- Risk controls: IP binding recommended (doc). Cancel-all-after and subaccounts: Not verified. Ours: the `lab/live.py` checks.
- Cost: fee schedule Not verified here; calm-lagoon quotes a 0 to 0.04% round trip. State exclusions per calm-lagoon: AK AS CT GA GU MP NY NC ND OH OR TX VI VT WA.
- Paper: the dry-run league.
- Verified: 2026-11-06. Sources: https://docs.binance.us/, https://www.binance.us/terms-of-use.

### Coinbase (Advanced Trade)

- Status: candidate.
- Strategy fit: Not a live bot venue: it keeps no exchange-side stop, Freqtrade does not support it, and the round trip is 1.0 to 1.8% (calm-lagoon). Read-only balance and manual trades only. Advanced Trade also lists US futures and derivatives (doc). Those are a possible short route once checked.
- API:
  - CDP API key with a JWT on private calls.
  - Permissions are `view`, `trade` and `transfer`, so transfer is separable (doc).
  - Rate limits: Not verified (sources conflict).
  - Sandbox mentioned without detail.
- Agent machine: no for data. The developer terms bar recording CDP data "through automated programs ... or any other method of screen scraping" (snippet).
- The person: keys, funding, every order.
- Sign-in and 2FA: Not verified.
- Risk controls: a key is scoped to one portfolio (calm-lagoon). IP allowlist: Not verified.
- Cost: Not verified here.
- Paper: the dry-run league.
- Verified: 2026-11-06. Sources: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api, https://docs.cdp.coinbase.com/coinbase-app/docs/rate-limits, https://www.coinbase.com/legal/developer-platform/terms-of-service.

### Gemini

- Status: candidate.
- Strategy fit: a second US venue with a sandbox. Freqtrade does not support it, so it would need ccxt directly.
- API:
  - Public calls 120/min, private calls 600/min, transfers 1 per 5 s per subaccount, with a 5-request burst queue (doc).
  - Sandbox at `api.sandbox.gemini.com` with test funds (snippet).
  - Key roles and withdraw separation: Not verified.
- Agent machine: ToS Not verified.
- The person: account, keys, funding.
- Sign-in and 2FA: Not verified.
- Risk controls: a heartbeat flag cancels orders when the session drops (snippet). Subaccounts (doc).
- Cost: Not verified.
- Paper: the Gemini sandbox.
- Verified: 2026-11-06. Sources: https://developer.gemini.com/rate-limit, https://developer.gemini.com/trading/rest-api/orders/heartbeat.

### Read-only key checklist (crypto exchanges)

Verified 2026-11-06 from each venue's docs. The person creates every key. No agent does, and none is created until the person decides to connect the venue. A key is read-only when no trade, cancel, deposit, withdraw or earn permission is ticked.

| Venue | Tick for the read key | Leave off | IP binding | Native kill switch (for the Stage 1 trade key) |
|---|---|---|---|---|
| Kraken | Query Funds; Query Open Orders & Trades; Query Closed Orders & Trades; Query Ledger Entries (doc) | Create & Modify Orders; Cancel/Close Orders; Deposit Funds; Withdraw Funds; Earn; Export Data | Recommended (doc). The settings are not documented on that page: Not verified | `CancelAllOrdersAfter`. The trade key also needs Create & Modify or Cancel/Close. Withdraw stays off |
| Binance.US | Security types USER_DATA and MARKET_DATA only, under Edit restrictions. Spot Trading off (doc) | Enable Spot Trading; any withdraw-capable route. A separate withdraw toggle is not documented: Not verified | Recommended but optional (doc) | None native. The watcher cancels per symbol and stops entries. Check `GET /api/v3/account` `canWithdraw` is false on the read key |
| Coinbase | `view` only (doc) | `trade`; `transfer` | Not verified | Not applicable: Coinbase is read-only or manual here (see its record) |
| Gemini | Auditor-style role, if the venue offers one: Not verified on the pages read | Trader; Fund Manager | Not verified | `require-heartbeat` on the trade key |

Checks after the person makes a read key, before it goes in SOPS:
- A balance call succeeds.
- A trade call is refused. Use `POST /api/v3/order/test` on Binance.US only if its docs say what it does: Not verified. Otherwise test the refusal with a deliberately invalid, far-from-market order on the real account, and only with the person's say-so.
- The key is stored under the exchange name in SOPS, as in [live.md](live.md), and never in the repo or rbw.
- The reconciliation in [live.md](live.md) uses this key and nothing else.

Not verified in this pass, for P2 follow-up: Kraken key expiry and nonce window; Gemini key roles and how `require-heartbeat` is enabled; Coinbase IP allowlist and rate limits; Binance.US pages past the first 200k characters. Sign-in and 2FA for all four, and the Kraken and Gemini terms on automation, are also still open.

Other US venues worth a record later: Bitstamp, Crypto.com. Not researched.

## Self-custody

### Trezor

- Status: held.
- Strategy fit: long-term holdings (the core hold in calm-lagoon), kept off exchanges.
- API:
  - `trezorctl get-public-node` returns the extended public key (xpub) for a watch-only wallet (doc).
  - With the xpub, balances, history and receive addresses can be read without the device, through Trezor Suite with auto-eject off (snippet) or a public explorer or our own node.
  - Trezor's public Blockbook servers (btc1/btc2.trezor.io), which answer xpub queries through `/api/v2/xpub/{xpub}` (Blockbook openapi.yaml), return a Cloudflare 403 to any client but Suite (tested 2026-11-06). So we derive the addresses ourselves and read them from Esplora (see "Watch-only read" below).
  - Building an unsigned transaction (PSBT) from the xpub: Not verified on a Trezor page.
- Agent machine: Trezor Suite on the mac-mini can do everything up to the confirmation: open the send screen, fill it, connect apps. Trezor Connect routes PIN and passphrase through a trezor.io popup (snippet).
- The person: every signature. Safe 3 (buttons) and Safe 5 (touchscreen) confirm each transaction on the device (snippet). That press is the point of the device, so no automation goes around it.
  - The seed and passphrase never go into rbw, a file, a screen or a photo.
- Sign-in and 2FA: the device PIN, entered by the person.
- Risk controls: the press itself. Ours: none needed for reads.
- Cost: no software fee seen (Not verified). Suite's buy, sell and swap route through partners (Invity: Banxa, MoonPay and others) at third-party-reported spreads of about 4.5 to 10% (snippet). Use an exchange instead.
- Paper: not needed. Reads are risk-free, and a send is always the person's.
- Verified: 2026-11-06. Sources: https://trezor.io/guides/trezorctl/using-trezorctl-commands, https://trezor.io/guides/trezor-suite/getting-to-know-trezor-suite, https://trezor.io/trezor-safe-3, https://trezor.io/trezor-safe-5, https://connect.trezor.io/9/, https://github.com/trezor/blockbook/blob/master/openapi.yaml, https://github.com/Blockstream/esplora/blob/master/API.md.

#### Watch-only read

`python -m lab.wallet` writes one `wallet_balances` row per account per run: confirmed and unconfirmed satoshis and the count of used addresses. It needs only the account's public key, which cannot sign, so the job can never move coins. `/accounts` shows the newest read of each account.

- Input: `WALLET_ACCOUNTS` (default `/etc/trader-wallet/accounts.tsv`), one `<name><TAB><key>` line per account. The key is the account public key Suite exports (its "Show public key" label is Not verified on a Trezor page): an xpub (legacy), ypub (SegWit), zpub (native SegWit), or a descriptor such as Suite's taproot `tr([fp/86'/0'/0']xpub.../<0;1>/*)`. Bitcoin mainnet only. The file is a Secret, because a public key reveals every address and balance of its account.
- How: `embit` derives the receive and change chains, each until 20 unused addresses in a row (the BIP44 gap limit). Each address is read from the Esplora API in `lab/wallet.toml`. The tests check the derivation against the BIP44, 49, 84 and 86 test vectors.
- Privacy: the Esplora host (blockstream.info by default) sees every address we ask about, the same exposure Suite's default backend has. To avoid it, point `base_url` at our own Esplora or mempool instance. The database holds the account name and never the key, and log lines and errors carry the HTTP status, never an address.
- Live check (2026-11-06): the public BIP84 test-vector zpub read its 40 used addresses from blockstream.info in 84 s at a 1 s gap. mempool.space returned 429 at 0.5 s.
- To run it, the person exports each account's public key from Suite (the device asks for a confirmation) and adds it to a SOPS Secret in home-config. A CronJob in `apps/freqtrade/` then runs the module hourly. Neither exists yet.

#### Signing runbook (the agent prepares, the person presses)

1. The agent writes down the send: why, the amount, and the destination. A destination that is one of our exchange deposit addresses is read from that exchange's API with its read key, never copied from chat or a clipboard. The fee rate comes from Esplora `/fee-estimates`.
2. On the mac-mini (`agent-machine`), the agent opens Trezor Suite, selects the account and fills in the Send form (address, amount, fee). It stops at the review step.
3. The person plugs the Trezor into the mac-mini and enters the PIN, and the passphrase if there is one, on the device. They check the full address and the amount shown on the device against step 1, then confirm on the device. If anything differs, they reject it on the device.
4. After the broadcast, the agent checks the next `lab.wallet` read (a negative unconfirmed balance, then lower confirmed sats) and the txid on Esplora.

The agent never types the PIN, the passphrase or the seed, never opens the recovery screens, and never starts a send that the person did not ask for. Trezor Suite is not installed on the mac-mini yet. Installing it is step 2's first run.

## API brokers

None of these has a strategy in the repo yet. Freqtrade trades crypto only. A broker would carry equity or ETF strategies, or crypto at Alpaca or Robinhood, once written and walked forward.

Only Alpaca and the Tastytrade sandbox can be practised on without money: IBKR paper needs a funded account and Schwab has no paper API. Alpaca is the one connected. No broker here needs a mac-mini re-auth today: Alpaca keys and Tastytrade refresh tokens do not expire, and the IBKR and Schwab logins stay the person's until their terms are read.

### Alpaca

- Status: candidate. The cheapest end-to-end paper target.
- Strategy fit: US equities and ETFs (core hold, rebalancing), and crypto.
- API:
  - Key ID and secret in headers; keys do not expire (doc). OAuth is not yet available for the Trading API (doc).
  - Live at `api.alpaca.markets`, paper at `paper-api.alpaca.markets`, with separate keys (doc).
  - Trading API 200 requests/min per account, paper and live counted apart (secondary: Alpaca support). Free data is IEX real-time, with SIP delayed 15 minutes.
  - Alpaca says it dropped the pattern-day-trader designation after FINRA's 2026-06-04 rule change (snippet).
  - Key scopes: no read-only Trading API key found, so a key pair can trade its account (Not verified). A paper key can only reach paper.
- Agent machine: not needed. Keys do not expire, so there is no re-auth.
- The person: opens the paper account (email only), creates the paper keys, and later opens and funds a live account.
- Sign-in and 2FA: TOTP. Required for live keys and live trading (secondary: Alpaca blog). For paper, sources conflict.
- Risk controls: cancel-all and close-all endpoints (see Kill switches).
- Cost: free paper and free IEX data. Algo Trader Plus data is $99/month (snippet). US stocks are commission-free, regulatory fees apply. Crypto starts at 0.15% maker, 0.25% taker (doc).
- Paper: a free paper account with $100k, its own keys, and about 10% of eligible fills partial. It fills only marketable orders and does not model dividends, queue position or regulatory fees (doc).
- Verified: 2026-11-06. Sources: https://docs.alpaca.markets/us/docs/authentication.md, https://docs.alpaca.markets/us/docs/paper-trading, https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, https://docs.alpaca.markets/us/reference/postorder.md, https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md, https://docs.alpaca.markets/us/reference/deleteallorders-1.md, https://docs.alpaca.markets/us/reference/deleteallopenpositions-1.md, https://docs.alpaca.markets/us/docs/crypto-fees, https://alpaca.markets/support/usage-limit-api-calls, https://alpaca.markets/learn/guide-to-multi-factor-authentication-mfa-account-security-for-alpaca-builders.

#### Paper connection

`lab/broker.py` is the path, and `AlpacaPaper` refuses any host but `paper-api.alpaca.markets`, so no config can reach a live account.

- `python -m lab.broker read` writes one `broker_balances` row: account status, equity, cash, buying power and the position count, or the error. Holdings themselves are not stored. `/accounts` shows the newest row and `trader_broker_equity` and `trader_broker_read_age_seconds` export it.
- `python -m lab.broker drill` rests one SPY share at $1.00 (`lab/broker.toml`), reads it back, cancels it and reads it back, writing to `demo_orders`. Outside market hours the order sits in `accepted`, which counts as resting. Whether a limit that far from the market is accepted is Not verified; the first drill settles it.
- Input: `ALPACA_PAPER_KEY_ID` and `ALPACA_PAPER_SECRET_KEY`. With none set, `read` exits 0 and writes nothing.
- Live since 2026-11-12 (home-config #832): `trader-broker-read` runs `read` hourly at :17 and `trader-broker-drill` runs the drill weekdays at 14:40 UTC, keys from SOPS Secret `trader-brokers` (the person's copy is rbw `trading/alpaca-paper`). First run: equity 100000.0 USD, 0 positions, and the drill's SPY order was seen resting and then canceled.

### Interactive Brokers

- Status: candidate.
- Strategy fit: everything (equities, options, futures, crypto) and the best real paper account. Heavier to run.
- API:
  - The TWS API needs TWS or IB Gateway running. The Client Portal Web API runs through a local gateway.
  - OAuth 1.0a for individuals: sources conflict. One IBKR page describes self-service RSA key upload with a session of about 24 hours; another says retail clients get only the gateway and OAuth 1.0a stays institutional (both snippet, pages returned 403).
  - A gateway session idles out without `/tickle`, sent about every 60 s, and resets at midnight (snippet).
  - Rate limit: 10/s or 50/s per username (sources conflict).
  - The API is not available on IBKR Lite (snippet).
- Agent machine: a gateway login needs 2FA each day, which is a machine candidate. IBKR says individuals log in by hand and automation needs a token scheme such as OAuth (snippet). Its terms on automating the login: Not verified, so the daily login stays the person's.
- The person: account, funding, and the daily gateway login, unless OAuth turns out to be open to individuals.
- Sign-in and 2FA: IB Key (mobile), a generic TOTP app, or a DSC+ device (accounts over $1M). SMS and email codes are temporary only (doc). TOTP therefore fits rbw.
- Risk controls: see Kill switches. API orders pass the same precautionary checks and can be rejected (snippet). Orders priced too far from the market are rejected, 10% for stocks (snippet).
- Cost: IBKR Pro, $0 minimum. Fixed pricing is $0.005/share with a $1 minimum. Real-time data is billed per exchange; delayed data is free (snippet).
- Paper: not free of money. "A paper trading account will not be opened until your account has been funded" (doc), so this path needs a funded live account, or possibly the free trial (snippet). It starts at $1,000,000 with its own login (doc).
- Verified: 2026-11-06. Sources: https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm, https://interactivebrokers.github.io/tws-api/classIBApi_1_1EClient.html, https://www.interactivebrokers.com/campus/glossary-terms/precautionary-settings/, https://ibkrguides.com/securelogin/sls/twofactorauth.htm, https://www.interactivebrokers.com/campus/ibkr-api-page/oauth-1-0a-extended/, https://www.interactivebrokers.com/docs/web-api/trading/usage-and-availability/pacing-limitations, https://www.interactivebrokers.com/en/pricing/commissions-home.php.

### Schwab

- Status: candidate.
- Strategy fit: equities and options, if the person wants a large US broker with an API.
- API:
  - Trader API at developer.schwab.com; individual apps are approved by hand in a few days.
  - OAuth: access token 30 minutes, refresh token 7 days and not extendable, then a browser re-login. The callback must be `127.0.0.1` on a port above 1024 (secondary: schwab-py; Schwab's pages returned 403).
  - 120 non-order calls/min, and an order rate set at registration (snippet).
  - Scopes: Not verified. No paper mode: orders hit a live account (snippet). paperMoney is only in thinkorswim.
- Agent machine: no, until checked. The online services terms bar software that logs on to Schwab or automates fetching data, except browsers and apps Schwab approves (snippet). The weekly OAuth re-login is the person's until Schwab's terms are read in full.
- The person: the weekly re-login, the account, the app registration.
- Sign-in and 2FA: Symantec VIP, mobile push, SMS (snippet). A plain TOTP app: Not verified.
- Risk controls: Not verified.
- Cost: API appears free (secondary). Commissions: Not verified.
- Paper: none found for the API, so connecting Schwab would mean a real account. Not connected until the person wants that.
- Verified: 2026-11-06. Sources: https://schwab-py.readthedocs.io/en/latest/auth.html (secondary), https://schwab-py.readthedocs.io/en/latest/getting-started.html (secondary), https://www.schwab.com/legal/terms (snippet, page errored), https://developer.schwab.com (403).

### Tastytrade

- Status: candidate.
- Strategy fit: options and futures.
- API:
  - Open API, 96 operations, covering equities, options, futures and crypto (doc).
  - OAuth2. The access token lasts 15 minutes; the refresh token does not expire. Scopes are `read`, `trade` and `openid`, so a read-only grant is possible. A personal app is limited to your own account (doc).
  - REST is throttled "by reasonable thresholds", with no published numbers. Repeated failed logins block the IP for about 8 hours (doc).
- Agent machine: not needed, because the refresh token does not expire.
- The person: account, the OAuth app, sandbox signup.
- Sign-in and 2FA: granting `read` or `trade` needs 2FA on the account, by authenticator app or SMS (doc). TOTP therefore fits rbw.
- Risk controls: see Kill switches.
- Cost: stocks and ETFs $0. Options $1 per contract to open, capped at $10 per leg, $0 to close. Futures $0.75 to $1.25 per contract. Crypto 1%, or 0.75% for BTC and ETH (doc). Sandbox price: Not verified.
- Paper: the cert sandbox at `api.cert.tastyworks.com`, signed up in the browser, with separate credentials. Unscripted symbols fill at the quote mid whatever the limit, and trades and positions clear every 24 hours, so it can prove an order path and little more. Market data returns 502 there (doc).
- Verified: 2026-11-06. Sources: https://developer.tastytrade.com/docs/sandbox, https://tastytrade.com/pricing/, https://developer.tastytrade.com/llms.txt, https://developer.tastytrade.com/, https://developer.tastytrade.com/docs/authentication/oauth2/, https://developer.tastytrade.com/docs/guides/rate-limits-and-backoff, https://developer.tastytrade.com/docs/faq/.

Also seen, to record later: Tradier (personal tokens that never expire, 60 trading calls/min, sandbox; snippet) and Webull OpenAPI (needs an approved US account with $100 and has a sandbox; snippet).

## Brokers without a full API

### Fidelity

- Status: held.
- Strategy fit: where the person's existing investments are. We read holdings into the book. We do not trade there.
- API: no public retail trading API (secondary). The read paths that respect Fidelity's terms are consented aggregators. We use SnapTrade:
  - SnapTrade's Fidelity integration is read only ("this integration does not support placing trades") and connects by OAuth: the person signs in on Fidelity's own site, and SnapTrade never sees the password (doc).
  - A Personal API key (client ID and consumer key) reads the person's own connections with no user registration. The default `read` connection type "supports data access only" (doc).
  - `GET /accounts` returns every connected account with `balance.total` (amount, currency), `status` and `is_paper`. Its data is cached and refreshed once a day (doc).
  - Rate limits: 250 requests/min per client ID, and 10 requests/min per account for Personal users where rolled out (doc).
  - Not chosen: Akoya (Fidelity's FDX network, sales-gated pricing, snippet) and CSV export by the person (works, but a person every time and nothing for the agent to schedule).
- Agent machine: no. Fidelity's terms bar software "designed to provide high-speed, automated, or repeated access" and any tool that would "automate or otherwise assist the process of obtaining, downloading, transferring, or transmitting any information", unless Fidelity approves it in writing (doc). Fidelity has blocked third-party screen scraping since 2023 (snippet). Driving fidelity.com from the mac-mini would break the terms on an account the person holds.
- The person: every order, creating the SnapTrade Personal key (the SnapTrade Dashboard requires 2FA first), and connecting Fidelity in SnapTrade's Connection Portal with a read connection.
- Sign-in and 2FA: Symantec VIP, possibly moving to generic TOTP apps (sources conflict). The OAuth sign-in supports 2FA (doc, SnapTrade).
- Risk controls: n/a, since we do not trade there. The read connection and our GET-only client (below) both stop an order.
- Cost: SnapTrade Personal users are "currently completely free", limited to one Personal client ID and consumer key (doc). Commercial plans are billed per connected user and are not needed.
- Paper: none at Fidelity. The SnapTrade sandbox (on Personal keys, enabled by default, at the bottom of the institution list) returns deterministic accounts, balances and positions, so the read path can be proved before Fidelity is connected (doc).
- Verified: 2026-11-06. Sources: https://prime.fidelity.com/Terms-of-Use, https://snaptrade.com/brokerage-integrations/fidelity-api, https://docs.snaptrade.com/docs/getting-started, https://docs.snaptrade.com/docs/personal-vs-commercial.md, https://docs.snaptrade.com/docs/request-signatures, https://docs.snaptrade.com/docs/sandbox.md, https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_listUserAccounts.md.

#### Read connection

`SnapTrade` in `lab/broker.py` is the path. It sends only GET, refuses any host but `api.snaptrade.com`, and has no order method.

- `python -m lab.broker read` writes one `broker_balances` row per connected account, named institution, account name and the last four of the number (`fidelity_individual_1234`), with `paper` from SnapTrade's `is_paper`. It stores the total value and status; holdings stay at SnapTrade. An account with no total is a row with an error.
- Input: `SNAPTRADE_CLIENT_ID` and `SNAPTRADE_CONSUMER_KEY`. With none set, the source is skipped.
- Cadence: once a day is enough, since SnapTrade refreshes once a day.
- Key deployed 2026-11-12 (home-config #832): the Personal key is in rbw `trading/snaptrade` and Secret `trader-brokers`, read hourly by `trader-broker-read`; `/api/v1/accounts` answers 200 with no accounts. It is live once the person connects the sandbox, then Fidelity with a read connection, and a Fidelity row appears on `/accounts`.

### Robinhood

- Status: candidate.
- Strategy fit: crypto through its official Crypto Trading API. Equities stay with the person.
- API:
  - Crypto Trading API at `trading.robinhood.com`, for Robinhood Crypto customers in the United States (doc).
  - Auth: an API key plus an Ed25519 signature over api key, timestamp, path (with query), method and body, in the `x-api-key`, `x-timestamp` and `x-signature` headers. A timestamp is valid for 30 s. The person makes the keypair and registers the public key in crypto account settings on web classic (doc).
  - Key scopes: the person picks the API actions a key may use. Accounts, holdings, orders, products and quotes are read actions, on v1 and v2 (doc). P5 asks for a key with only accounts, holdings and quotes.
  - 100 requests/min per account, bursts to 300, token bucket (doc).
  - Reads used: `GET /api/v1/crypto/trading/accounts/` (status, buying power), `/holdings/` (paged by `next`), and `/api/v1/crypto/marketdata/best_bid_ask/` (midpoint `price` per pair) (doc).
  - No equities API found.
  - Robinhood may revoke API authorization at any time (snippet).
- Agent machine: Not verified; treat it as no. The API covers the reads.
- The person: the keypair and key, funding, every order, equities.
- Sign-in and 2FA: authenticator app, SMS and passkeys (snippet, UK pages). The API key itself has no 2FA step.
- Risk controls: per-key action scopes (doc). IP allowlist and key expiry: Not verified. Ours: the read-only key plus a GET-only client.
- Cost: the API has no listed fee. Trading fees come from fee tiers on 30-day volume, counted only for v2 orders (doc). Reads cost nothing.
- Paper: none found. Read-only only.
- Verified: 2026-11-06. Sources: https://docs.robinhood.com/crypto/trading/ (the page's own OpenAPI bundle), https://robinhood.com/us/en/support/articles/crypto-api, https://robinhood.com/gb/en/support/articles/twofactor-authentication.

#### Read-only connection

`RobinhoodCrypto` in `lab/broker.py` sends only GET, refuses any host but `trading.robinhood.com` (a holdings `next` page too), and has no order method, so even a key with trade actions could not trade from here.

- `python -m lab.broker read` writes one `robinhood_crypto` row with `paper` false: status, buying power as cash, the count of non-zero holdings, and equity as cash plus each holding at its `<asset>-USD` midpoint. A holding with no price leaves equity empty and says which.
- Input: `ROBINHOOD_API_KEY` and `ROBINHOOD_PRIVATE_KEY` (the base64 32-byte Ed25519 seed). With none set, the source is skipped.
- A real account's balance stays on `/accounts`, behind sign-in. The exporter sends only its read age, never its equity.
- Not yet run against Robinhood: tested only against a fake API, with the signature checked against the docs' recipe. It is live once the person creates a read-only key, adds it to rbw as `trading/robinhood` and a SOPS Secret, a home-config CronJob runs `read`, and a Robinhood row appears on `/accounts`.

## Prediction markets

Data facts, fees as backtested, and adapter notes are in [markets.md](markets.md). This section adds accounts, terms and automation.

### Kalshi

- Status: candidate. Opening the account and a read-only key is tr-50; caps and state of residence are tr-51; live is tr-48.
- Strategy fit: the only prediction market with demo orders in the plan (P7 in [markets.md](markets.md)).
- API:
  - Any member may trade by API (doc).
  - Requests are signed with Ed25519 (the default) or RSA-PSS (doc).
  - Basic tier: 200 reads/s and 100 writes/s; Advanced 300/300 on request; higher tiers by volume share (doc).
  - Demo at `external-api.demo.kalshi.co`, with mock funds and separate credentials (doc).
- Agent machine: not needed. The Developer Agreement limits API use to "facilitating a members own trading", requires backoff and bounded retries, and lets Kalshi cut access without notice (doc). Browser automation: Not verified.
- The person: account, funding, keys, and any caps set on the account.
- Sign-in and 2FA: SMS or an authenticator app (TOTP), asked about every 30 days (snippet). TOTP fits rbw.
- Risk controls: order groups with a 15 s fill cap; self-exclusion and funding caps that cannot be lifted early (doc).
- Cost: no API fee found. Trading fees are in [markets.md](markets.md).
- Paper: the demo exchange. The drill (`lab/markets/kalshi_trade.py`, [markets.md](markets.md), Demo orders) runs daily at 14:50 UTC as `trader-kalshi-demo-drill` since 2026-11-12 (home-config #832), with the key from Secret `trader-kalshi-demo` (rbw `trading/kalshi-demo`). First run: one contract seen resting and then canceled. The live key (rbw `trading/kalshi`) reads the real balance and is not deployed.
- Verified: 2026-11-06. Sources: https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf, https://docs.kalshi.com/getting_started/api_keys, https://docs.kalshi.com/getting_started/rate_limits, https://docs.kalshi.com/getting_started/demo_env, https://help.kalshi.com/en/articles/13823787-how-do-i-use-2fa-two-factor-authentication-verification-codes.

### Polymarket US

- Status: candidate. API status checked 2026-11-06 (P6): the Retail API trades today; no account is open.
- Strategy fit: a second regulated US prediction market for cross-venue links (`market_links` in [markets.md](markets.md)).
- API:
  - A retail trading API exists: verify identity in the iOS app, then create a Key ID and Secret Key at polymarket.us/developer (doc). An invite code may be needed for the app (doc: "need an invite code ... email support@polymarket.us").
  - Signed headers `X-PM-Access-Key`, `X-PM-Timestamp` (ms) and `X-PM-Signature`, an Ed25519 signature of timestamp + method + path, within 30 s of the server (doc). Official Python and TypeScript SDKs (`polymarket_us`, `polymarket-us`).
  - Trading at `api.polymarket.us`: `POST /v1/orders` (limit and market), `POST /v1/order/preview` (validates an order and shows expected fills without placing it), `GET /v1/order/{id}`, `POST /v1/order/{id}/cancel`, cancel-all, batches of 20, and a private websocket for order, position and balance events (doc). Public data at `gateway.polymarket.us`.
  - 25 requests/s per IP and Cloudflare location on the Retail API, shared across keys (doc, rate-limits page; this settles the earlier 20 vs 25). Orders not processed within 5 s are rejected as `Order Processing Max Delay Exceeded`, which is safe to resubmit (doc).
  - No retail sandbox. Preprod (`api.preprod.polymarketexchange.com`, Auth0 tokens) is the Institutional API only (doc, trader-guide/environments).
- Agent machine: not needed; the API covers orders. Whether the US terms allow bots: Not verified. The Polymarket terms page bars scraping robots and VPNs (doc), but it names a different entity than the US exchange (QCX LLC), so it may not be the US terms.
- The person: identity check in the iOS app, keys, funding.
- Sign-in and 2FA: Apple, Google or email sign-in, with the same method each time or the API key can break (doc). 2FA belongs to the Apple or Google account; Polymarket sends verification codes (doc, learn/account/protect). No TOTP of its own found, so nothing for rbw but the key.
- Risk controls: cancel-all (kill-switch map above), deposit limits and self-exclusion (snippet).
- Cost: no API fee found. Trading fees are in [markets.md](markets.md).
- Paper: none from the venue, and `preview` is not a paper fill. Our backtest engine and the paper league are the paper path. An order client waits until the person opens an account, and then it is real money, so it comes in behind `lab/live.py` (docs/live.md), never as a drill.
- Verified: 2026-11-06. Sources: https://docs.polymarket.us/api-reference/authentication, https://docs.polymarket.us/api-reference/orders/overview, https://docs.polymarket.us/api-reference/rate-limits, https://docs.polymarket.us/trader-guide/environments, https://docs.polymarket.us/learn/account/protect.

### Polymarket global

- Status: data only, permanently.
- Strategy fit: price data and links.
- API: a CLOB with wallet-signed orders (doc).
- Agent machine: no.
- The person: nothing to do. US persons are close-only on the site and the API, and the terms allow "NO EXCEPTIONS" for restricted persons (doc). Do not open or trade an account there, and do not use a VPN.
- Sign-in and 2FA: n/a.
- Risk controls: n/a.
- Cost: none, since we read public data only.
- Paper: n/a.
- Verified: 2026-11-06. Sources: https://docs.polymarket.com/api-reference/geoblock, https://polymarket.com/tos.

### PredictIt

- Status: data only.
- Strategy fit: politics prices for links.
- API: one read-only feed, `predictit.org/api/marketdata/all/` (doc). No trading API.
- Agent machine: no. The terms reportedly ban bots and scraping (secondary; clause not quoted).
- The person: any trade.
- Sign-in and 2FA: Not verified.
- Risk controls: a per-contract cap (sources conflict).
- Cost: 10% of profits and a 5% withdrawal fee (secondary).
- Paper: none.
- Verified: 2026-11-06. Sources: https://www.predictit.org/api/marketdata/all/.

### Manifold

- Status: data, and the order drill is built (P6); it runs once the account key exists (sheet row tr-118).
- Strategy fit: a free practice venue an agent can trade end to end.
- API:
  - `POST /v0/bet` places market and limit bets in mana (play money), with an `Authorization: Key` header; one key per account (doc). `limitProb` (0.01 to 0.99, whole percents) makes it a limit order, and `expiresMillisAfter` has Manifold cancel it on its own (doc). The minimum bet is 1 mana (source, `common/src/economy.ts`).
  - `POST /v0/bet/cancel/{id}` cancels a limit bet; `GET /v0/bets?userId=&contractId=` reads it back, since there is no one-bet route; `GET /v0/me` is the account and its balance (doc).
  - 500 requests/min per IP (doc).
  - `dryRun` checks a bet without placing it.
- Agent machine: not needed. Bots are allowed: "Building bots, automated trading systems, algorithmic tools, and integrations that interact with Manifold" (doc). Scraping outside the API is not allowed.
- The person: creates the account and its API key.
- Sign-in and 2FA: Not verified.
- Risk controls: play money; each drill bet expires on its own (kill-switch map above).
- Cost: free.
- Paper: the venue itself.
- Verified: 2026-11-06. Sources: https://docs.manifold.markets/api, https://github.com/manifoldmarkets/manifold/blob/main/common/src/api/schema.ts.

#### Order drill

`lab/markets/manifold_trade.py` is the path, and `ManifoldBets` refuses any host but `api.manifold.markets`, so the key goes nowhere else.

- `python -m lab.markets.manifold_trade balance` prints the account's mana.
- `python -m lab.markets.manifold_trade drill [--ticker MARKET_ID]` picks an open binary mana market priced at `min_prob` (10%) or more, rests a 1-mana YES limit bet at 1% with a 10-minute expiry (`[demo.manifold]` in `lab/markets.toml`), reads it back, cancels it and reads it back, writing each step to `demo_orders` as venue `manifold`. It exits 1 unless the bet was seen open with nothing filled and then canceled. `/markets/book` shows the row.
- Input: `MANIFOLD_API_KEY`.
- Not yet run against Manifold: tested only against a fake API, because no key exists. It is live once the person makes the key, adds it to rbw as `trading/manifold` and a SOPS Secret, the drill runs from home-config `apps/freqtrade/`, and a canceled `manifold` row appears on `/markets/book`.

## Sportsbooks

We read sportsbook odds through The Odds API ([markets.md](markets.md), adapter `lab/markets/odds.py`). We never place a bet at a sportsbook by any automated means. Every bet is the person's, on their own device, inside a legal state.

### DraftKings and FanDuel

- Status: candidate.
- Strategy fit: implied odds as a signal and a fair-price reference for Kalshi and Polymarket US sports markets (the `gap` family in [markets.md](markets.md)); any bet placed by hand.
- API: no public retail API for either (snippet). Their odds reach us only through The Odds API.
- Agent machine: no.
  - FanDuel's fantasy (DFS) and Faceoff terms bar any "robot, spider, scraper, sniping software or other automated means" without written permission (doc). Its sportsbook terms were not found; they reportedly bar bots too (snippet).
  - DraftKings' Fair Play page bars browser scripts and bots for fantasy (snippet). Its sportsbook terms returned 403: Not verified.
  - Even where a clause is unread, a bot risks the account, and the location check below ties every bet to a real device in a legal state. The seahorse plan already excludes both.
- The person: account, deposits, every bet, every withdrawal.
- Sign-in and 2FA:
  - DraftKings requires 2FA at login in legal states (snippet). Methods: Not verified (sources conflict on SMS only versus SMS or an authenticator app).
  - FanDuel offers an authenticator app or SMS (snippet); regulation requires it in CT, IL, NJ, OH, PA, TN, VT and WY (snippet).
  - Both check location before a bet. DraftKings uses GeoComply, the "Player Location Check" plugin on desktop or precise location on mobile (secondary). FanDuel's vendor: Not verified.
  - No rbw entry. 2FA and location are the person's, since no bet is automated.
- Risk controls (native):
  - DraftKings: deposit limits (daily, weekly, monthly), wager and max-per-bet limits, time limits, cool-off, self-exclusion for 1 or 5 years across all DraftKings products (snippet). Cool-off length: Not verified.
  - FanDuel: deposit, loss, wager, per-slip and time limits, a timeout, and self-exclusion from one year or indefinite. A lowered limit applies at once, a raised one after a cooldown (snippet).
  - Ours: none needed, since no code bets. A person who bets should set a deposit limit first.
- Cost: none to read beyond the Odds API tier.
- Paper: none from the books. Our backtest engine (`lab/markets/backtest.py`) against recorded odds is the paper path.
- Event contracts: both now sell CFTC-route prediction products beside the sportsbook.
  - FanDuel Predicts, with CME Group, launched 2025-12-22 in AL, AK, SC, ND and SD, rolling out through 2026. Sports contracts are offered only where online sports betting is not legal (doc, Flutter press release). Public API: none found.
  - DraftKings Predictions launched 2025-12-19 in 38 states on CME markets, moving to Railbird, the CFTC exchange DraftKings bought in 2025-10 (snippet). Public API: none found.
  - Both are person only, like Robinhood's event contracts.
- Verified: 2026-11-06. Sources: https://www.fanduel.com/terms, https://www.fanduel.com/faceoff-terms, https://www.draftkings.com/fantasy-fair-play-commitment, https://help.draftkings.com/hc/en-us/articles/4405232295827-How-do-I-limit-my-access-to-DraftKings-US, https://support.fanduel.com/s/article/How-do-I-set-account-limits, https://flutter.com/news-media/press-releases/fanduel-and-cme-group-launch-fanduel-predicts/.

#### Odds read

The read is live since 2026-11-12: home-config CronJob `trader-markets-recorder-odds-api` runs `python -m lab.markets.recorder odds_api` at :07 every 6 hours with `ODDS_API_KEY` from SOPS Secret `trader-odds-api` (rbw `trading/odds-api`). The first sweep recorded 154 quotes and used 3 credits.

- Pricing (doc, https://the-odds-api.com, 2026-11-06): free for 500 credits/month ("most bookmakers"); $30 for 20K, $59 for 100K, $119 for 5M, $249 for 15M, all bookmakers.
- Credits (doc, v4 guide): `/odds` costs 1 per market per region; `/scores` with `daysFrom` costs 2. One sweep of `[venues.odds_api]` as configured (one sport, h2h, region `us`) is 3 credits.
- Cadence by tier: the free tier covers one sport every 6 hours (4 runs a day, about 360 credits a month). Hourly needs the $30 tier (about 2,200 a month); every 5 min is about 26,000 a month, past the $30 tier.
- The default 5-min recorder must not run it: `odds_api` stays out of `[recorder] default_venues`. Every 6 hours is about 360 credits a month of the free 500. Check it with `SELECT started_at, seen, ok FROM market_sweeps WHERE venue = 'odds_api' ORDER BY id DESC LIMIT 3;`; credits left are in the `x-requests-remaining` response header. A paid tier and a faster cadence are the person's money call.
- The key rides in the query string (doc), so the adapter re-raises HTTP errors without the URL.

### Exchange-style sports venues

Peer-to-peer sports venues with an order book and an official trading API. Each became a CFTC exchange in 2026, so they sit closer to Kalshi than to a sportsbook. The Odds API's `us_ex` region already reads Novig and ProphetX prices (doc: betopenly, kalshi, limitless, novig, polymarket, polymarket_us, prophetx).

#### ProphetX

- Status: candidate.
- Strategy fit: sports contracts with a book, an alternative to Kalshi sports; a second venue for the `gap` family.
- API: any KYC-approved account can make Trading API keys in account settings, with no application. Higher rate limits and RFQ or parlay quoting need entity onboarding. The Market Data API is separate, by request (doc). Sandbox: `api.sandbox.prophetx.dev/partner` (snippet).
- Agent machine: not needed. Terms 3.5 bar software-assisted play "with the exception of programmatic trading via the ProphetX API, which is available to all Users", and ProphetX may revoke API access at any time (doc). Bots go through the API only.
- The person: account, KYC, funding, keys.
- Sign-in and 2FA: Not verified.
- Regulation: CFTC DCM and DCO since 2026-06, no longer sweepstakes (secondary). State list: Not verified.
- Risk controls: key generation is refused while the account is self-excluded (doc). Cancel-all and dead-man switch: Not verified.
- Cost: fees are on a page not read: Not verified.
- Paper: the sandbox.
- Verified: 2026-11-06. Sources: https://docs.prophetx.co/docs/requesting-api-access, https://www.prophetx.co/lobby/t-c/terms, https://docs.prophetx.co/docs/getting-started.

#### Novig

- Status: candidate.
- Strategy fit: as ProphetX; pre-game straight trades carry no fee.
- API: a signed API (Ed25519 or P-256), with `POST /v3/echo` to check a signature. A management key, made in the profile, opens and funds subaccounts but cannot place orders; each subaccount has its own trading key (doc). No application is mentioned. Parlays are not in the API (snippet).
- Agent machine: not needed; bot terms Not verified.
- The person: account, funding, the management key.
- Sign-in and 2FA: Not verified.
- Regulation: Ludlow Exchange became a CFTC DCM on 2026-06-16, clearing through Bitnomial, and replaced the sweepstakes product around 2026-08. Offered in 47 states and DC (not NV, AZ, MI), age 21 and up (secondary).
- Risk controls: the subaccount split is itself a limit, since the management key cannot trade, and a subaccount holds only what it is funded with.
- Cost: no fee pre-game; a live taker fee of c x p x (1-p), with c reported as 0.03, 0.06 or 0.10 (snippet, conflicting), and makers credited half. Not verified until read on Novig's own page.
- Paper: none confirmed; a sandbox is Not verified. Until then, our backtest engine.
- Verified: 2026-11-06. Sources: https://docs.novig.com, https://predictionscout.com/reviews/novig-review/.

#### Not available by API

- Sporttrade: stopped US wagering 2026-05-25 and closed accounts 2026-06-26; a CFTC application is pending with no relaunch date (secondary, https://www.rotowire.com/news/sporttrade-stops-us-wagering-as-it-exits-market-116232).
- Robinhood event contracts run on Kalshi, with no API for them (snippet). Underdog's sports markets are backed by Kalshi and Crypto.com (snippet). Crypto.com event contracts: no API found. Trade the underlying Kalshi market instead ([Kalshi](#kalshi)).
- Kalshi and Polymarket US sports contracts are in their own records above.
