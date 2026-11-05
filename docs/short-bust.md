# Shorting a bust

Research only, from `lab/research/bust.py`, run on 2026-11-05. Nothing here trades, and `BustShort` stays out of `user_data/strategies`, so no job or bot finds it. Both baskets count in the trial register (migration 017).

## Result

A rule that shorts trend breaks does not pay. Over the whole history it lost 39% of the wallet on BTC, ETH and SOL, and 26% on five AI tokens, before funding. With shorts paying 0.03% every 8 hours, about 33% a year, those become 59% and 54%. It made money in some falls, but never much: +13% in the 60% BTC fall of 2022. False alarms outside any fall cost 23% and 27%.

So the recommendation is: do not build an automated short. If the human wants protection against an AI bust, the plain route is a small long put position at a broker, where the most it can lose is what it cost.

## The test

`BustShort` shorts a new 20-day low under the 200-day average while BTC is under its own 200-day average, and covers above the 50-day average, with a 15% stop and no leverage. One backtest per basket on okx daily swap candles from 2020-01-01 to 2026-11-04, with a fixed stake per coin, so a fall's result does not depend on the ones before it. Fees are 0.1% a side, which includes a 0.05% slippage guess. A fall is BTC down 25% from a peak, ended by a 25% rise from the low. A trade belongs to the fall it opened in.

`majors` is BTC, ETH and SOL. `ai` is NEAR, GRT, RENDER, TAO and WLD.

| Fall | BTC | majors | majors, shorts pay | Trades | ai | ai, shorts pay | Trades |
|---|---|---|---|---|---|---|---|
| 2020-02-14 to 2020-03-12 | -54% | not tested | not tested | 0 | not tested | not tested | 0 |
| 2021-01-08 to 2021-01-27 | -25% | 0% | 0% | 0 | 0% | 0% | 0 |
| 2021-04-13 to 2021-07-20 | -53% | -34.3% | -35.4% | 7 | -6.0% | -6.2% | 2 |
| 2021-11-08 to 2022-01-22 | -48% | +1.9% | +0.3% | 1 | +2.5% | +1.2% | 2 |
| 2022-03-29 to 2022-06-18 | -60% | +13.3% | +10.4% | 1 | +10.9% | +8.0% | 5 |
| 2022-08-13 to 2022-11-21 | -35% | -6.3% | -10.2% | 4 | +5.4% | +1.4% | 6 |
| 2024-03-13 to 2024-09-06 | -26% | -7.6% | -9.1% | 3 | -16.5% | -19.0% | 8 |
| 2025-01-21 to 2025-04-08 | -28% | -1.9% | -2.9% | 1 | -15.0% | -17.1% | 7 |
| 2025-10-06 to 2026-02-05 | -50% | +12.5% | +8.9% | 3 | +13.6% | +7.3% | 10 |
| 2026-05-10 to 2026-06-30 | -29% | +6.3% | +4.6% | 1 | +5.8% | +2.7% | 3 |
| Outside every fall | | -22.8% | -25.9% | 10 | -27.0% | -32.6% | 18 |
| Whole history | | -38.8% | -59.3% | 31 | -26.3% | -54.3% | 61 |

Returns are on the whole wallet. Won, after funding: 19% of majors trades and 25% of ai trades. Each column adds up to its whole-history row.

## Limits

- No AI crash is in this data, so this is a scenario. BTC falls stand in for it, and the AI tokens may not fall the way AI stocks would.
- The 2020 crash is untested: the swap candles start 2020-01-01, so the 200-day average did not exist yet. The first trade is 2021-05-20.
- The AI basket is today's survivors, chosen with hindsight, and most of it listed after 2021. Tokens that died are missing, which flatters a long and may understate a short.
- Funding is assumed. okx keeps about 3 months of funding history, so the two columns are bounds I chose. Neither was measured. The deduction is on stake for days held.
- Daily candles understate a stop: a short stopped at -15% can gap past it, and a squeeze can run far beyond it.
- One rule with one parameter set. A tuned rule would look better here, and that would mean little.
- The last fall sits inside the lockbox months, and this research reads them. It tunes nothing and nothing here feeds the score, but those months are no longer unseen for this rule.

## Which route, if any

okx is the research venue only: US residents are not served (medium confidence, no primary source). None of the routes below works with Freqtrade, so each would need its own code.

| Route | Most you can lose | Notes | Source |
|---|---|---|---|
| Long puts on IBIT, QQQ, SMH, NVDA | What you paid | Simplest defined loss. Alpaca offers long puts by API at options level 2. | snippet |
| Inverse ETFs: BITI (-1x, 1.02%), SBIT (-2x, 0.95%), SOXS (-3x, about 1.00%) | What you paid | Daily reset, so they decay when held. BITI and SBIT primary, SOXS and the AI inverses snippet. 63 leveraged single-stock ETFs closed in 2026 (snippet), so a fund can go away. | mixed |
| Coinbase US perpetual-style futures | No fixed limit | Nano BTC 0.01, ETH 0.10, since 2025-07-21, Advanced Trade API for eligible US clients (primary). Fees, leverage and retail access unverified. | mixed |
| Kraken US perps on Bitnomial | No fixed limit | Announced 2026-06-15, $0.0113 per contract a side (primary). Funding schedule conflicts across its pages; API coverage unverified. | mixed |
| CME Micro Bitcoin futures | No fixed limit | 0.1 BTC, margin about $2,000 to $2,800 (snippet), so an account of about $6,000 or more is an estimate. | snippet |

Freqtrade's futures venues are Binance, Bitget, Bybit, Gate.io, Hyperliquid, krakenfutures (international), and OKX. None of them is one of these US routes.

Equity puts and inverse ETFs need data this lab does not hold, so they stay untested. This is not investment or tax advice.

## Run it

`python lab/research/bust.py <userdir>` inside the image. It downloads into the userdir once, about 20 minutes.
