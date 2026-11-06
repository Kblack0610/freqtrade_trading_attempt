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
| Alpaca | candidate | API broker | API | API | Alpaca paper account | P4 |
| Interactive Brokers | candidate | API broker | API | API | IBKR paper account | P4 |
| Schwab | candidate | API broker | API | API | Not verified | P4 |
| Tastytrade | candidate | API broker | API | API | cert sandbox | P4 |
| Fidelity | held | broker, no API | aggregator only | person | none | P5 |
| Robinhood | candidate | broker | API (crypto only) | API (crypto), person (rest) | none | P5 |
| Kalshi | candidate (tr-50) | prediction market | API | API | demo exchange | P6, account tr-50, live tr-48 |
| Polymarket US | candidate | prediction market | API | API | backtest engine only | P6 |
| Polymarket global | data only | prediction market | API | never (US is close-only) | n/a | none |
| PredictIt | data only | prediction market | API (one feed) | person | none | P6 |
| Manifold | data | play money | API | API, bots allowed | the venue is play money | P6 |
| DraftKings, FanDuel | candidate | sportsbook | Odds API | person | none | P7 |
| Novig, ProphetX | candidate | exchange-style sportsbook | API | API, to verify | ProphetX sandbox | P7 |

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
| Kraken | `CancelAllOrdersAfter` dead-man timer: re-arm every 15 to 30 s with a 60 s timeout. Futures has `/cancelallordersafter` | snippet |
| Gemini | Heartbeat flag on the key: a missed heartbeat cancels open orders | snippet |
| Alpaca | `DELETE /v2/orders` cancels all orders. `DELETE /v2/positions?cancel_orders=true` closes everything | snippet |
| Kalshi | Order groups auto-cancel when a 15 s fill cap is hit. Self-exclusion and funding caps cannot be lifted early | doc |
| Polymarket global | `DELETE /cancel-all` | doc |
| Binance.US, Coinbase, IBKR, Schwab, Tastytrade, Polymarket US | Not verified | |

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

Other US venues worth a record later: Bitstamp, Crypto.com. Not researched.

## Self-custody

### Trezor

- Status: held.
- Strategy fit: long-term holdings (the core hold in calm-lagoon), kept off exchanges.
- API:
  - `trezorctl get-public-node` returns the extended public key (xpub) for a watch-only wallet (doc).
  - With the xpub, balances, history and receive addresses can be read without the device, through Trezor Suite with auto-eject off (snippet) or a public explorer or our own node.
  - Building an unsigned transaction (PSBT) from the xpub: Not verified on a Trezor page.
- Agent machine: Trezor Suite on the mac-mini can do everything up to the confirmation: open the send screen, fill it, connect apps. Trezor Connect routes PIN and passphrase through a trezor.io popup (snippet).
- The person: every signature. Safe 3 (buttons) and Safe 5 (touchscreen) confirm each transaction on the device (snippet). That press is the point of the device, so no automation goes around it.
  - The seed and passphrase never go into rbw, a file, a screen or a photo.
- Sign-in and 2FA: the device PIN, entered by the person.
- Risk controls: the press itself. Ours: none needed for reads.
- Cost: no software fee seen (Not verified). Suite's buy, sell and swap route through partners (Invity: Banxa, MoonPay and others) at third-party-reported spreads of about 4.5 to 10% (snippet). Use an exchange instead.
- Paper: not needed. Reads are risk-free, and a send is always the person's.
- Verified: 2026-11-06. Sources: https://trezor.io/guides/trezorctl/using-trezorctl-commands, https://trezor.io/guides/trezor-suite/getting-to-know-trezor-suite, https://trezor.io/trezor-safe-3, https://trezor.io/trezor-safe-5, https://connect.trezor.io/9/.

## API brokers

None of these has a strategy in the repo yet. Freqtrade trades crypto only. A broker would carry equity or ETF strategies, or crypto at Alpaca or Robinhood, once written and walked forward.

### Alpaca

- Status: candidate. The cheapest end-to-end paper target.
- Strategy fit: US equities and ETFs (core hold, rebalancing), and crypto.
- API:
  - Key ID and secret in headers; keys do not expire (doc). OAuth is not yet available for the Trading API (doc).
  - Live at `api.alpaca.markets`, paper at `paper-api.alpaca.markets`, with separate keys (doc).
  - Trading API 200 requests/min per account (snippet). Free data is IEX real-time, with SIP delayed 15 minutes.
  - Alpaca says it dropped the pattern-day-trader designation after FINRA's 2026-06-04 rule change (snippet).
  - Key scopes: Not verified.
- Agent machine: not needed.
- The person: opens the paper account (email only), creates the paper keys, and later opens and funds a live account.
- Sign-in and 2FA: MFA available. Method: Not verified.
- Risk controls: cancel-all and close-all endpoints (see Kill switches).
- Cost: free paper and free IEX data. Algo Trader Plus data is $99/month (snippet). Commissions: Not verified.
- Paper: a free paper account with $100k, its own keys, and about 10% of orders randomly partly filled. Dividends and borrow fees are not simulated (doc).
- Verified: 2026-11-06. Sources: https://docs.alpaca.markets/docs/authentication, https://docs.alpaca.markets/docs/paper-trading.

### Interactive Brokers

- Status: candidate.
- Strategy fit: everything (equities, options, futures, crypto) and the best real paper account. Heavier to run.
- API:
  - The TWS API needs TWS or IB Gateway running. The Client Portal Web API runs through a local gateway. Self-service OAuth 1.0a lets an individual upload an RSA key, and a session lasts about 24 hours (snippet).
  - A gateway session idles out after 5 minutes without `/tickle`, and resets at midnight.
  - Rate limit: 10/s or 50/s per username (sources conflict).
  - The API is not available on IBKR Lite (snippet).
- Agent machine: a gateway login needs 2FA each day, which is a machine candidate. Prefer OAuth 1.0a, which avoids the daily login. IBKR terms on automating its login: Not verified, so until checked the daily login is the person's.
- The person: account, OAuth key upload, and the daily gateway login if OAuth is not used.
- Sign-in and 2FA: IB Key (mobile), a generic TOTP app, or a DSC+ device (accounts over $1M). SMS and email codes are temporary only (doc). TOTP therefore fits rbw.
- Risk controls: Not verified.
- Cost: IBKR Pro, $0 minimum. Fixed pricing is $0.005/share with a $1 minimum. Real-time data is billed per exchange; delayed data is free (snippet).
- Paper: a paper account with API access from activation (snippet).
- Verified: 2026-11-06. Sources: https://ibkrguides.com/securelogin/sls/twofactorauth.htm, https://www.interactivebrokers.com/campus/ibkr-api-page/oauth-1-0a-extended/, https://www.interactivebrokers.com/docs/web-api/trading/usage-and-availability/pacing-limitations, https://www.interactivebrokers.com/en/pricing/commissions-home.php.

### Schwab

- Status: candidate.
- Strategy fit: equities and options, if the person wants a large US broker with an API.
- API:
  - Trader API at developer.schwab.com; individual apps are approved by hand in a few days.
  - OAuth: access token 30 minutes, refresh token 7 days, then a browser re-login (snippet; Schwab's page returned 403).
  - 120 non-order calls/min, and an order rate set at registration (snippet).
  - Scopes and a paper API: Not verified. paperMoney is only in thinkorswim.
- Agent machine: no, until checked. The online services terms bar software that logs on to Schwab or automates fetching data, except browsers and apps Schwab approves (snippet). The weekly OAuth re-login is the person's until Schwab's terms are read in full.
- The person: the weekly re-login, the account, the app registration.
- Sign-in and 2FA: Symantec VIP, mobile push, SMS (snippet). A plain TOTP app: Not verified.
- Risk controls: Not verified.
- Cost: API appears free (secondary). Commissions: Not verified.
- Paper: Not verified for the API.
- Verified: 2026-11-06. Sources: https://www.schwab.com/legal/terms (snippet, page errored), https://developer.schwab.com (403).

### Tastytrade

- Status: candidate.
- Strategy fit: options and futures.
- API:
  - Open API, 96 operations, covering equities, options, futures and crypto (doc).
  - OAuth2. The access token lasts 15 minutes; the refresh token does not expire. Scopes are `read`, `trade` and `openid`. A personal app is limited to your own account (doc).
  - REST is throttled "by reasonable thresholds", with no published numbers (doc).
- Agent machine: not needed, because the refresh token does not expire.
- The person: account, the OAuth app, sandbox signup.
- Sign-in and 2FA: required for other users of an app (doc). Methods: Not verified.
- Risk controls: Not verified.
- Cost: Not verified.
- Paper: the cert sandbox at `api.cert.tastyworks.com`, with separate credentials (doc). How well it simulates fills: Not verified.
- Verified: 2026-11-06. Sources: https://developer.tastytrade.com/, https://developer.tastytrade.com/docs/authentication/oauth2/, https://developer.tastytrade.com/docs/guides/rate-limits-and-backoff, https://developer.tastytrade.com/docs/faq/.

Also seen, to record later: Tradier (personal tokens that never expire, 60 trading calls/min, sandbox; snippet) and Webull OpenAPI (needs an approved US account with $100 and has a sandbox; snippet).

## Brokers without a full API

### Fidelity

- Status: held.
- Strategy fit: where the person's existing investments are. We read holdings into the book. We do not trade there.
- API: no public retail trading API (secondary). The read paths that respect Fidelity's terms are consented aggregators:
  - Akoya, Fidelity's FDX data network, OAuth and read-only, with sales-gated pricing (snippet).
  - SnapTrade, which lists Fidelity (snippet; read-only or trading there: Not verified).
  - CSV export from the site, done by the person: Not verified.
- Agent machine: no. Fidelity's terms bar software "designed to provide high-speed, automated, or repeated access" and any tool that would "automate or otherwise assist the process of obtaining, downloading, transferring, or transmitting any information", unless Fidelity approves it in writing (doc). Fidelity has blocked third-party screen scraping since 2023 (snippet). Driving fidelity.com from the mac-mini would break the terms on an account the person holds.
- The person: every order, any export, and approving an aggregator connection.
- Sign-in and 2FA: Symantec VIP, possibly moving to generic TOTP apps (sources conflict).
- Risk controls: n/a, since we do not trade there.
- Cost: SnapTrade's Build tier is $0 for up to 5 connections. Launch is $100/month plus $1 to $2 per connected user (doc). Akoya: sales-gated.
- Paper: none. Read-only only.
- Verified: 2026-11-06. Sources: https://prime.fidelity.com/Terms-of-Use, https://snaptrade.com/pricing, https://snaptrade.com/faq.

### Robinhood

- Status: candidate.
- Strategy fit: crypto through its official Crypto Trading API. Equities stay with the person.
- API:
  - Crypto Trading API: an API key plus an Ed25519 signature; you register the public key in the credentials portal.
  - 100 requests/min, with bursts to 300 (snippet; the docs page is an empty app shell).
  - No equities API found.
  - Robinhood may revoke API authorization at any time (snippet).
- Agent machine: Not verified; treat it as no.
- The person: keys, funding, equities orders.
- Sign-in and 2FA: authenticator app, SMS and passkeys (snippet, UK pages).
- Risk controls: Not verified.
- Cost: Not verified.
- Paper: none found.
- Verified: 2026-11-06. Sources: https://docs.robinhood.com/crypto/trading/ (snippet), https://robinhood.com/gb/en/support/articles/twofactor-authentication.

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
- Paper: the demo exchange.
- Verified: 2026-11-06. Sources: https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf, https://docs.kalshi.com/getting_started/api_keys, https://docs.kalshi.com/getting_started/rate_limits, https://docs.kalshi.com/getting_started/demo_env, https://help.kalshi.com/en/articles/13823787-how-do-i-use-2fa-two-factor-authentication-verification-codes.

### Polymarket US

- Status: candidate.
- Strategy fit: a second regulated US prediction market for cross-venue links (`market_links` in [markets.md](markets.md)).
- API:
  - A retail trading API exists: verify identity in the iOS app, then create a key at polymarket.us/developer (doc).
  - Signed headers, with timestamps within 30 s of the server (doc).
  - Trading at `api.polymarket.us`; public data at `gateway.polymarket.us`.
  - 25 requests/s per IP (doc; a secondary source says 20).
  - No sandbox documented.
- Agent machine: Not verified. The Polymarket terms page bars scraping robots and VPNs (doc), but it names a different entity than the US exchange (QCX LLC), so it may not be the US terms.
- The person: identity check in the iOS app, keys, funding.
- Sign-in and 2FA: Apple, Google or email sign-in. 2FA: Not verified.
- Risk controls: deposit limits and self-exclusion (snippet).
- Cost: a taker fee formula is reported (secondary). Not verified.
- Paper: none from the venue. Our backtest engine is the paper path.
- Verified: 2026-11-06. Sources: https://docs.polymarket.us/api-reference/authentication, https://docs.polymarket.us/api-reference/introduction, https://docs.polymarket.us/api-reference/rate-limits.

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

- Status: data today, with an order adapter planned.
- Strategy fit: a free practice venue an agent can trade end to end.
- API:
  - `POST /v0/bet` places market and limit bets in mana (play money), with an `Authorization: Key` header; one key per account (doc).
  - 500 requests/min per IP (doc).
  - `dryRun` checks a bet without placing it.
- Agent machine: not needed. Bots are allowed: "Building bots, automated trading systems, algorithmic tools, and integrations that interact with Manifold" (doc). Scraping outside the API is not allowed.
- The person: creates the account and its API key.
- Sign-in and 2FA: Not verified.
- Risk controls: n/a (play money).
- Cost: free.
- Paper: the venue itself.
- Verified: 2026-11-06. Sources: https://docs.manifold.markets/api.

## Sportsbooks

We read sportsbook odds through The Odds API ([markets.md](markets.md)): free for 500 credits/month; $30, $59, $119 and $249 tiers (doc, https://the-odds-api.com/). Buying a tier is the person's call.

### DraftKings and FanDuel

- Status: candidate.
- Strategy fit: implied odds as a signal; any bet placed by hand.
- API: no public retail API.
- Agent machine: no.
  - FanDuel's terms bar any "robot, spider, scraper, sniping software or other automated means" without written permission (doc, Faceoff terms).
  - Its sportsbook terms reportedly bar bots and programs that place bets automatically (snippet).
  - DraftKings reportedly bars bots, scrapers and browser scripts (snippet; page returned 403).
  - Automating either would risk the account, as the seahorse plan already decided.
- The person: every bet.
- Sign-in and 2FA: Not researched.
- Risk controls: each book's deposit limits and self-exclusion. Not researched.
- Cost: the Odds API tier, if a strategy needs more than the free tier.
- Paper: none from the books. Our backtest engine against recorded odds is the paper path.
- Verified: 2026-11-06. Sources: https://www.fanduel.com/faceoff-terms, https://the-odds-api.com/.

### Novig and ProphetX

- Status: candidate. Exchange-style US sports venues with APIs.
- Strategy fit: sports contracts with an order book, an alternative to Kalshi sports.
- API:
  - Novig: a signed API (Ed25519 or P-256). A management key funds subaccounts but cannot place orders; each subaccount has its own trading key (doc).
  - ProphetX: Trading, Market Data and other APIs, with a sandbox at `api.sandbox.prophetx.dev` (doc). Whether a plain retail account gets the Trading API: Not verified.
- Agent machine: ToS Not verified.
- The person: accounts, funding, keys.
- Sign-in and 2FA: Not verified.
- Risk controls: Novig's subaccount split is itself a limit, since a management key cannot trade.
- Cost: Not verified.
- Paper: the ProphetX sandbox.
- Verified: 2026-11-06. Sources: https://docs.novig.com, https://docs.prophetx.co/docs/getting-started.

Not offered to retail by API today: Sporttrade (closed, waitlist), Robinhood and Crypto.com event contracts (no public API found).
