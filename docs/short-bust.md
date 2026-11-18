# Shorting a bust

Research only, from `lab/research/bust.py`, first run on 2026-11-05 with funding assumed, rerun on 2026-11-07 with measured funding. Nothing here trades, and `BustShort`, `DonchianLongShort` and `CarverLongShort` stay out of `user_data/strategies`, so no job or bot finds them. Every basket counts in the trial register (migrations 017, 032 and 060). Carver's long-short trend, AFTS 6 and 8 (`CarverLongShort64` and `CarverLongShort16`), was added on 2026-11-17.

## Result

A rule that shorts trend breaks does not pay. Over the whole history it lost 39% of the wallet on BTC, ETH and SOL, and 26% on five AI tokens. It made money in some falls, but never much: +13% in the 60% BTC fall of 2022. False alarms outside any fall cost 23% and 27%.

Funding does not change that. Measured funding (`funding_rates`, collected by `lab/futures.py`) starts 2024-01-01 on kucoin for BTC, ETH, SOL and NEAR, and 2026-08-03 on okx; GRT, RENDER, TAO and WLD have none. On average the rate was positive, so longs paid shorts. Over the trades it covers (11 of 31 majors, 10 of 61 ai), funding moved the result by under half a point: majors +5.6% to +5.7%, ai -18.8% to -19.2%. The constant "shorts pay 0.03% every 8 hours" bound of the first run, which turned -39% into -59%, was far too harsh, and it is gone.

So the recommendation stands: do not build an automated short. If the human wants protection against an AI bust, the plain route is a small long put position at a broker, where the most it can lose is what it cost.

The trend family's Donchian ensemble run both ways (`DonchianLongShort`, the `TrendDonchian` settings on swap candles) made +262% on majors and +14% on ai over the whole history, but nearly all of it came from trades outside every fall and before 2024, where no funding is measured (+253% of the majors' +260% outside the falls). Over the trades with measured funding, 2024 on, it lost: majors -6.6% before funding and -11.5% after (its long legs paid), ai -3.1% and -3.4%. Inside the falls it made +1.2% and +4.8%. It is one parameter set, so this describes what happened and shows no edge.

## The test

`BustShort` shorts a new 20-day low under the 200-day average while BTC is under its own 200-day average, and covers above the 50-day average, with a 15% stop and no leverage. One backtest per basket on okx daily swap candles from 2020-01-01 to 2026-11-06, with a fixed stake per coin, so a fall's result does not depend on the ones before it. Fees are 0.1% a side, which includes a 0.05% slippage guess. A fall is BTC down 25% from a peak, ended by a 25% rise from the low. A trade belongs to the fall it opened in.

Funding per trade is the sum of the measured settlements between its open and close times its stake, received by a short and paid by a long when the rate is positive. okx's own series is used where it starts by the trade's open, else kucoin's for the same swap (a proxy: a different venue's crowd). A trade neither covers gets no funding guessed, and is counted only in the "No funding" columns. Freqtrade's own funding fees, from the funding candles it downloads, are taken out first, so nothing is counted twice.

`majors` is BTC, ETH and SOL. `ai` is NEAR, GRT, RENDER, TAO and WLD.

| Fall | BTC | majors | Trades | majors, measured, no funding | Same, measured funding | Measured trades | ai | Trades | ai, measured, no funding | Same, measured funding | Measured trades |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2020-02-14 to 2020-03-12 | -54% | not tested | 0 | | | 0 | not tested | 0 | | | 0 |
| 2021-01-08 to 2021-01-27 | -25% | 0% | 0 | | | 0 | 0% | 0 | | | 0 |
| 2021-04-13 to 2021-07-20 | -53% | -34.3% | 7 | | | 0 | -6.0% | 2 | | | 0 |
| 2021-11-08 to 2022-01-22 | -48% | +1.9% | 1 | | | 0 | +2.5% | 2 | | | 0 |
| 2022-03-29 to 2022-06-18 | -60% | +13.3% | 1 | | | 0 | +10.9% | 5 | | | 0 |
| 2022-08-13 to 2022-11-21 | -35% | -6.3% | 4 | | | 0 | +5.4% | 6 | | | 0 |
| 2024-03-13 to 2024-09-06 | -26% | -7.6% | 3 | -7.6% | -7.5% | 3 | -16.5% | 8 | -7.4% | -7.5% | 3 |
| 2025-01-21 to 2025-04-08 | -28% | -1.9% | 1 | -1.9% | -1.9% | 1 | -15.0% | 7 | -3.8% | -3.8% | 2 |
| 2025-10-06 to 2026-02-05 | -50% | +12.5% | 3 | +12.5% | +12.3% | 3 | +13.6% | 10 | -1.1% | -1.4% | 2 |
| 2026-05-10 to 2026-06-30 | -29% | +6.3% | 1 | +6.3% | +6.5% | 1 | +5.7% | 3 | | | 0 |
| Outside every fall | | -22.8% | 10 | -3.7% | -3.7% | 3 | -27.0% | 18 | -6.5% | -6.5% | 3 |
| Whole history | | -38.8% | 31 | +5.6% | +5.7% | 11 | -26.4% | 61 | -18.8% | -19.2% | 10 |

Returns are on the whole wallet. Won: 19% of majors trades and 26% of ai trades. Each column adds up to its whole-history row.

`DonchianLongShort`, same baskets, candles and fees:

| Window | majors | Trades | measured, no funding | measured funding | Measured trades | ai | Trades | measured, no funding | measured funding | Measured trades |
|---|---|---|---|---|---|---|---|---|---|---|
| Inside the falls | +1.2% | 27 | -13.5% | -13.0% | 19 | +4.8% | 25 | +0.4% | +0.7% | 4 |
| Outside every fall | +260.3% | 40 | +6.9% | +1.4% | 14 | +9.1% | 50 | -3.5% | -4.1% | 5 |
| Whole history | +261.5% | 67 | -6.6% | -11.5% | 33 | +13.9% | 75 | -3.1% | -3.4% | 9 |

## Carver long-short

Carver's slow and fast trend run both ways (AFTS 6 and 8, [carver-afts.md](carver-afts.md#part-one-basic-directional)) do no better, and the short half adds nothing. `CarverLongShort64` is EWMAC(64, 256) long above 0 and short below it, with the stake set by volatility alone. `CarverLongShort16` is EWMAC(16, 64) the same way, with the stake set by its strength, and without the book's buffer (that needs position adjustment). Each long half is the forecast family's own class (`CarverEwmac64` and `CarverTrendStrength16`, read from `user_data/strategies/forecast/family.toml`), so only the short half is new here. The baskets, candles, fees and funding are the same as above. The run is from 2026-11-17, with candles to 2026-11-16, which adds one more fall (2026-09-21 to 2026-11-14), so `DonchianLongShort` was rerun beside them.

Whole history, all trades, with no funding counted, then the trades from 2024 on with measured funding, before and after it:

| Rule | Basket | Side | No funding | Trades | Measured, no funding | Measured funding | Measured trades |
|---|---|---|---|---|---|---|---|
| CarverLongShort64 | majors | both | +36.8% | 34 | -38.7% | -39.5% | 17 |
| CarverLongShort64 | majors | shorts | -15.9% | 19 | -4.2% | -2.6% | 10 |
| CarverLongShort64 | ai | both | -0.7% | 31 | -5.7% | -5.8% | 9 |
| CarverLongShort64 | ai | shorts | -2.5% | 19 | -4.6% | -4.1% | 5 |
| CarverLongShort16 | majors | both | +30.7% | 123 | -0.1% | -0.5% | 61 |
| CarverLongShort16 | majors | shorts | +0.7% | 60 | +0.6% | +0.7% | 31 |
| CarverLongShort16 | ai | both | +13.5% | 143 | +0.3% | +0.3% | 19 |
| CarverLongShort16 | ai | shorts | +4.1% | 71 | +0.3% | +0.3% | 10 |
| DonchianLongShort | majors | both | +268.2% | 70 | +0.0% | -4.8% | 36 |
| DonchianLongShort | majors | shorts | -0.8% | 36 | -0.5% | +0.2% | 20 |
| DonchianLongShort | ai | both | +12.9% | 78 | -4.1% | -4.4% | 9 |
| DonchianLongShort | ai | shorts | -1.1% | 41 | -2.9% | -2.5% | 5 |

What it shows:

- Over the measured years, neither Carver rule made money: AFTS 6 lost 39.5% on majors after funding, nearly all of it in its long half (7 long trades, none won), and AFTS 8 ended within a point of zero on both baskets.
- The short halves, the only new part, made -4.1% to +0.7% over the measured years on 5 to 31 trades. Over the whole history the best was +4.1% (AFTS 8 on ai) and the worst -15.9% (AFTS 6 on majors). Shorting below a zero trend forecast did not pay here.
- Funding moved each Carver row by under 2.5 points and the Donchian longs by 5.7. Shorts received it on average and longs paid it, because the rates were mostly positive.
- As with Donchian, the whole-history gains on majors are long trades before 2024, where no funding is measured.

So Carver's long-short trend is NO-GO on this evidence, like `DonchianLongShort`. These are two parameter sets on two baskets, counted as four trials (migration 060), so this result describes what happened and claims no edge either way. The long halves keep running in the nightly analysis as forecast-family classes, and their numbers are on their own strategy pages.

## Limits

- No AI crash is in this data, so this is a scenario. BTC falls stand in for it, and the AI tokens may not fall the way AI stocks would.
- The 2020 crash is untested: the swap candles start 2020-01-01, so the 200-day average did not exist yet. The first trade is 2021-05-20.
- The AI basket is today's survivors, chosen with hindsight, and most of it listed after 2021. Tokens that died are missing, which flatters a long and may understate a short.
- Funding is measured only from 2024-01-01 (kucoin, four coins) and 2026-08-03 (okx), so most trades, and every 2021-2022 fall, have none. kucoin stands in for okx until okx's own series starts. Funding is charged on stake; freqtrade would use the mark-price notional.
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

`python lab/research/bust.py <userdir> [BustShort|DonchianLongShort|CarverLongShort64|CarverLongShort16]` inside the image, with `DATABASE_URL` set for the funding. It downloads into the userdir once, about 20 minutes.
