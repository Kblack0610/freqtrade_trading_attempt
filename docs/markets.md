# Markets: venue reference

The facts `lab/markets/` is built on, one section per venue. Checked 2026-11-05; each claim names its source and how sure we are of it. Nothing here trades money. Every adapter is read-only until P7, and P7 trades only on the Kalshi demo exchange. Plan: `id-like-to-setup-abundant-seahorse` (epic tr-37). Brief: `docs/briefs/2026-11-05-markets.md` in the lab project.

Confidence: **doc** means read on the venue's own page, **secondary** means a law firm, tracker or news page, and **snippet** means a search summary only. A snippet claim is re-checked before code relies on it.

## Summary

| Venue | Role for us | Public reads | History from the venue | Demo | US resident may trade |
|---|---|---|---|---|---|
| Kalshi | data, demo orders in P7, live in P9 behind the decide row | REST without a key; websocket needs a key | yes, `/historical/*` | yes | yes, state litigation on sports contracts |
| Polymarket US | data | REST without a key | price history only, 45-day retention | institutional Preprod only | yes, Nevada blocked |
| Polymarket global | data only | REST and websocket without a key | price history | none | no: the US is close-only |
| PredictIt | data | one snapshot endpoint | none | none | yes, politics only, no trading API |
| Manifold | data, play money | REST and websocket without a key | bets API plus dumps to 2024-07 | play money | play money |
| The Odds API | sportsbook implied odds | API key (free tier 500 credits/month) | paid plans, from 2020-06 | n/a | we never bet at a sportsbook |

Open markets counted 2026-11-05: Kalshi 178,883 open with combos excluded (`/markets?status=open&mve_filter=exclude`, 179 pages of 1000); Polymarket global more than 2,100 (Gamma refuses an offset past 2,100; third parties put it at 12k to 20k); PredictIt 148 markets with 542 contracts. Manifold and Polymarket US were not counted.

## Kalshi

API (doc, https://docs.kalshi.com/getting_started/api_environments.md):
- REST `https://api.elections.kalshi.com/trade-api/v2` (also `external-api.kalshi.com`).
- Websocket `wss://api.elections.kalshi.com/trade-api/ws/v2`. The handshake needs an API key even for public channels (doc, websocket-connection.md). P1 polls REST until a key exists (sheet row tr-50).
- Market data without a key: `/series/{t}`, `/events/{t}`, `/markets`, `/markets/{t}/orderbook`, `/markets/trades`, `/series/{s}/markets/{t}/candlesticks` (doc, quick_start_market_data.md).
- `/markets` pages by cursor, up to 1000 a page, and filters by `status`, `series_ticker`, `event_ticker`, `mve_filter` and the `min/max_*_ts` fields. Prices are dollar strings (`yes_bid_dollars`) and sizes fixed-point strings (`volume_fp`); `liquidity_dollars` was removed 2026-10-01 (doc, changelog).
- Candlesticks: `period_interval` 1, 60 or 1440 minutes, with bid, ask and trade OHLC, volume and open interest.

Rate limits (doc, https://docs.kalshi.com/getting_started/rate_limits): Basic tier 200 reads/s and 100 writes/s, with most requests costing 10 tokens. A 429 has no `Retry-After`, so back off exponentially. We got 429s on an unpaced count of `/markets`; a 0.5 s gap between pages was clean. Websocket: 200 connections up to Expert.

Fees (secondary, quoting the fee schedule PDF effective 2026-07-07; the PDF itself returned 429):
- Taker: `ceil(M * 0.07 * C * P * (1 - P))`, M = 1 by default. Example: 100 contracts at 50c costs $1.75; at 10c or 90c, $0.63.
- Maker: `ceil(M * 0.0175 * C * P * (1 - P))`, only on the series in the schedule's non-standard table; maker fees went live 2026-08-20 (doc, changelog).
- Rounding: the model fee is ceiled to 6 decimals and the overpayment is rebated per order (doc, fee_rounding.md). The backtester takes the fill's `fee_cost` as the authority where it has one, and the fee model is tested against the PDF's own examples once fetched.
- No deposit, settlement or membership fee (doc, help.kalshi.com fees article, 2026-08-24).

Demo (doc, https://docs.kalshi.com/getting_started/demo_env): REST `https://demo-api.kalshi.co/trade-api/v2`, websocket `wss://demo-api.kalshi.co/trade-api/ws/v2`, account at https://demo.kalshi.co/ with mock funds and separate keys. Demo prices do not track the real book, so P7 uses demo only to prove the order path, never to score a strategy.

History (doc, historical_data.md): `GET /historical/cutoff` gives the timestamps where records move from the live endpoints to `/historical/markets`, `/historical/markets/{t}/candlesticks` and `/historical/trades`. The live endpoints stop returning them after the move, so a backfill reads both sides of the cutoff. No earliest date is stated and there is no bulk download; P4 measures how far back it goes.

Terms (secondary): API use is under the Developer Agreement, which we have not read. Own-account automated trading looks allowed; redistributing market data does not. trader.kblab.me is sign-in only, which keeps it out of redistribution, and P8 re-checks against the agreement.

Legality (secondary and snippet, 2026-11-05; https://www.dlapiper.com/en/insights/publications/2026/09/legal-status-at-odds-tracking-developments-in-prediction-markets-and-sports-betting, https://www.cbssports.com/prediction/news/prediction-market-legal-states/, https://defirate.com/prediction-markets/legal/):
- Kalshi is a CFTC DCM. The fight is over sports contracts, and the federal circuits are split.
- Against Kalshi: Ninth Circuit (Nevada) 2026-08-28, rehearing denied 2026-11-04. Sixth Circuit (Ohio, Tennessee) 2026-09-25. Ohio fined Kalshi $5M.
- For Kalshi: Third Circuit (New Jersey) 2026-04; New Jersey's cert petition is No. 26-299, with Kalshi's response due 2026-12-07.
- Pending: Fourth Circuit (Maryland), argued 2026-05-07. Massachusetts SJC, argued 2026-05. Second Circuit (New York, Connecticut) denied Kalshi injunctions pending appeal 2026-11-03. Illinois, Iowa, Utah (criminal information against the founders), Arkansas, Missouri, Louisiana, Michigan, Washington and California (tribal lands) are in court in some form.
- CFTC: a proposed rule amending Rule 40.11 was published 2026-06-10. It would allow scores, point spreads, season stats and advancement contracts, and bar injury, officiating and in-game action contracts. No final rule; one source puts comments due 2026-11-18, another says they closed 2026-07-27.
- Congress: S.4160 and H.R. 9856 would bar sports contracts on CFTC venues. Both are in committee.
- What this means for us: data is legal everywhere. Which contracts a live stage may trade depends on the state of residence, which is decide row tr-51.

## Polymarket US

The CFTC-designated exchange (QCX LLC, amended order 2025-11-25; snippet), separate from global, with its own keys.

- Public data without a key: `https://gateway.polymarket.us`, including `GET /v1/markets`, book, BBO, settlement and `GET /v1/price-history` (doc, https://docs.polymarket.us/api-reference/introduction). Price history is built from quotes (best ask, 1 - best bid) rather than trades. It is 1 min for the last 1h/6h, 5 min for 1D and 3 h beyond.
- Limits: 20 requests/s per IP on the public gateway with a 30 s cache. Authenticated retail keys are 20 or 25 requests/s; the rate-limits page and the 2026-04-10 changelog disagree (doc).
- Fees from 2026-10-28 (doc, https://docs.polymarket.us/fees): `Theta * C * p * (1 - p)`, taker Theta 0.0695 (table tennis 0.10), banker's rounding to the cent; maker rebates and taker rebates by 30-day volume tier. Third-party fee guides quote old rates.
- Retention (doc, changelog 2026-07-13): market data 45 days, orders 90 days, executions 7 days. The venue cannot be the history source, so the recorder is.
- Sandbox: Preprod exists for the institutional API only, by contact (doc, environments.md). No retail demo, so Polymarket US is never in P7.
- States: Nevada blocked by court order; Massachusetts, Maryland and others contested (snippet, aggregator sites). No official state list found.

## Polymarket global

Read-only prices. A US resident must not trade it: the US is close-only on both site and API, and VPN use is barred (doc, https://polymarket.com/tos effective 2026-08-11; https://docs.polymarket.com/api-reference/geoblock).

- Hosts (doc): Gamma `https://gamma-api.polymarket.com`, CLOB `https://clob.polymarket.com`, data `https://data-api.polymarket.com`, websocket `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Price history: `GET /v2/prices-history` on the data host since 2026-09-04. The CLOB v1 route retired 2026-11-02, so older tutorials are broken (doc, changelog). CLOB V2 went live 2026-04-28 with pUSD collateral.
- Limits (doc, per 10 s, Cloudflare queues rather than rejects): Gamma `/markets` 300, `/events` 500; CLOB `/book` 1,500; data `/v2/prices-history` 200, `/v2/trades` 300.
- Gamma `/markets` pages by `offset` and refused offset 2,100 with a 422 on 2026-11-05, so a full sweep needs another path (events, or a keyset route); P2 finds it.
- Fees (doc, https://docs.polymarket.com/trading/fees): taker `C * feeRate * p * (1 - p)`, makers 0. The rate depends on category: crypto 0.07, sports and economics 0.05, politics and finance 0.04, geopolitics 0. Each market's `feeSchedule` object is the authority.
- Terms: market-data distributors may not use site data without a written agreement. Our use is private research, and the adapter reads only the documented API.

## PredictIt

- One endpoint, `https://www.predictit.org/api/marketdata/all/` (and `/markets/{id}`), no key, with best buy and sell prices per contract and the last trade (doc, fetched). No depth, trades, history, websocket or trading API.
- Rate limit not published; secondary sources range from 1 a second to 1 a minute, and the data refreshes about once a minute. Poll once a minute.
- Fees: 10% of profit on winning trades; the 5% withdrawal fee was dropped 2026-09-23 (secondary, nextpredict.io).
- Status (doc, CFTC Letter 25-20, 2025-07-14): runs under no-action relief through a non-profit. The trader cap per contract is gone and the position limit, formerly $850, is now the FECA contribution limit (about $3,500). Political contracts only.
- History: none from the venue. The recorder's snapshots are the history.

## Manifold

Play money (mana); the sweepcash real-money model ended 2025-03-28 (snippet).

- API `https://api.manifold.markets`, no key for reads, 500 requests/min per IP (doc, https://docs.manifold.markets/api). `/v0/markets` and `/v0/bets` page up to 1000; `/v0/market-probs` takes up to 100 ids. No price-history endpoint, so history is rebuilt from bets.
- Websocket `wss://api.manifold.markets/ws`.
- Dumps (doc, https://docs.manifold.markets/data): bets, markets and comments from 2021-12 to 2024-07, non-commercial use. Our use is private research.
- Use: calibration and base-rate studies, and a free paper venue. Its prices are not money prices, so a gap against Kalshi is not an arbitrage.

## Sportsbook odds: The Odds API

Chosen over OpticOdds, SportsDataIO and Sportradar, whose prices are unpublished or need a sales call and whose storage terms we could not read (secondary).

- Pricing (doc, https://the-odds-api.com/): free 500 credits/month, $30 for 20K, $59 for 100K, $119 for 5M, $249 for 15M. A call costs 1 credit per market per region; the sports and events lists are free.
- Coverage (doc): US, UK, EU and AU books, plus a `us_ex` group with Kalshi, Polymarket, Polymarket US, Novig and ProphetX.
- Cadence (doc, update-intervals.html): 60 s pre-match and 40 s in-play for main markets, 5 min for futures.
- History: paid plans, 10 credits per region per market, from 2020-06-06, 5-min snapshots from 2022-09.
- Terms (doc, terms-and-conditions.html): storing data indefinitely and using it in analytics is allowed; reselling or re-serving it as a feed is not.
- Sizing: one sport, one market, one region every 5 min is 288 credits a day, about 8,600 a month, so anything past a single sport needs a paid key. That is a human money decision for P3.
- We never place a bet at a sportsbook. FanDuel and DraftKings terms bar automated betting (secondary; the pages returned 403), and Betfair lists the US as a prohibited territory (snippet, clause 4.1.6).

## What this changes in the plan

- Target load: about 180k open markets across venues, up from the 10k the plan assumed, almost all on Kalshi. A 5-min sweep is still cheap: about 180 Kalshi list requests at 1000 a page, well inside the 200 reads/s Basic tier. The naive row count is about 52M a day, so change-only parquet in MinIO is the right store and Postgres holds only market metadata.
- The websocket for watched markets needs a Kalshi API key, so P1 polls REST until tr-50 is done.
- Polymarket US keeps only 45 days of market data, so P4's backfill for it is whatever the recorder has kept.
- Only Kalshi has a retail demo, so P7's order path is Kalshi only, as planned.

## Not verified

- The Kalshi fee schedule PDF and Developer Agreement text: both returned 429.
- Whether the Kalshi rate table includes the 2026-09-24 uplift for upper tiers.
- PredictIt's own terms, rate limit and state list (the site renders in JS).
- The Polymarket US bot terms, and whether its markets websocket needs a key.
- How far back Kalshi `/historical` and Polymarket `/v2/prices-history` reach.
- Court status past each source's date. Everything under Legality comes from trackers and law-firm alerts; no docket was read.
