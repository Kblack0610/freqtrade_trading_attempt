# Carry from recorded funding

Research only, from `lab/futures/research/carry.py`, first run on 2026-11-17. It measures Carver's carry strategies (AFTS 10, 11, 15, 16 and 20, [carver-afts.md](carver-afts.md)) on the funding we record, and asks two things: what long spot plus a short perpetual would have earned, and whether funding tells a long-only spot account anything. Nothing here trades. Each rule is a row in the trial register (migration 059).

## Result

Carry paid in 2024 and has not paid since. Holding long spot and a short perpetual in every coin, all the time, earned 4.2% a year on capital from 2024-01-02 to 2026-04-30. Almost all of that came in 2024 (+9.8% a year), then +0.9% in 2025 and -2.7% a year from January to April 2026. From January to April 2026 funding was negative on 14 of the 18 coins.

Being picky does not help. Each gate Carver's method suggests did worse than holding every coin, because funding changes sign too often: switching on the last day's rate paid 9.7% a year in fees and lost 3.9%. Holding only the third of coins with the highest funding lost 1.2%. The smoothed rate (AFTS 15) earned 2.5%, still under the plain harvest.

Funding does not forecast spot returns in a way a long-only account can use. Coins with the lowest funding did beat those with the highest over the next week (+1.4%, interval +0.3% to +2.5%), but only because of KCS and ZEC, the two coins whose funding stays negative. Without them the spread is +0.4% over a week and -0.1% over a month, and both intervals cover zero. A coin's funding against its own history predicts nothing either. The trend filter on funding (AFTS 11, long-only reading) and the 60/40 mix of trend and carry (AFTS 16) have Sharpe intervals that overlap the trend sleeve's and each other's, so neither is distinguishable from trend alone.

So the carry avenue stays blocked, and for two reasons now. The first is the missing short route. The second is that, measured, the trade has paid nothing since 2024. Revisit it when a rerun shows the always-on harvest above about 8% a year on capital over a quarter (2024 ran at +9.8%) and a short route exists. A funding filter for the spot league is a NO-GO on this data.

## The test

Funding is kucoin's recorded series (`funding_rates`, collected by `lab/futures/funding.py`), summed by UTC day, for the 18 coins on the kucoin whitelist; it starts 2024-01-01 (KCS 2025-01-26). okx's series starts 2026-08-03, inside the lockbox, so it is not used. Spot prices are kucoin's daily closes. The window ends at the lockbox start in `lab/crypto/suggest.toml` (2026-05-01), so those months stay unseen.

Each coin gets one equal slot of the sleeve, filled when the rule holds it and in cash when it does not, so one coin joining or leaving does not resize the others. Every position held on a day is decided from funding and closes through the day before. A harvest is long spot and short perpetual at equal notional and receives the rate when it is positive. Its capital is twice the notional, the spot plus the same again as 1x perpetual margin, so returns are on that. Fees are 0.1% per leg per side, kucoin's spot taker, which leaves room for slippage on the cheaper perpetual leg.

The smoothed rate is the mean of exponential averages over 5, 20, 60 and 120 days, Carver's carry spans. The trend sleeve holds a coin while the AFTS 9 EWMAC mix (`CarverTrendMix` settings, speeds 8 to 64, multiplier 1.13) is above 0. Sharpe intervals are 90%, from a 10-day block bootstrap (`lab/metrics.sharpe_ci`). Forecast spreads use 95% intervals in blocks as long as the horizon, since overlapping forward windows share days.

### Funding by coin

| Coin | Funding a year | Days positive | Lowest year |
|---|---|---|---|
| AAVE | +10.2% | 66% | -1.0% (2026) |
| AVAX | +10.3% | 62% | -1.3% (2026) |
| BNB | +3.2% | 59% | -0.6% (2026) |
| BTC | +8.9% | 71% | -2.7% (2026) |
| DOGE | +13.9% | 73% | -1.5% (2026) |
| DOT | +8.9% | 66% | -13.1% (2026) |
| ETH | +10.4% | 74% | -3.2% (2026) |
| KCS | -22.0% | 40% | -53.4% (2026) |
| LINK | +13.2% | 73% | +1.0% (2026) |
| LTC | +10.6% | 67% | -0.7% (2026) |
| NEAR | +12.2% | 65% | -3.3% (2026) |
| SHIB | +5.1% | 48% | -8.8% (2026) |
| SOL | +12.9% | 72% | -5.9% (2026) |
| TRX | +4.8% | 59% | -4.7% (2026) |
| UNI | +9.9% | 66% | +0.4% (2026) |
| XMR | +14.0% | 73% | +8.3% (2026) |
| XRP | +15.6% | 76% | -0.2% (2026) |
| ZEC | -8.6% | 54% | -38.9% (2025) |

Funding is per unit of notional, before the capital split. 2026 is January to April.

### Rules

| AFTS | Rule | Return a year | Sharpe | Sharpe 90% interval | Fees a year | Max drawdown |
|---|---|---|---|---|---|---|
| 10 | Harvest every coin, always | +4.2% | +6.62 | +4.40 to +8.70 | +0.0% | 1.6% |
| 15 | Harvest while the smoothed rate is above 0 | +2.5% | +5.40 | +3.63 to +7.12 | +1.0% | 1.1% |
| 15 | Harvest while the last day's rate is above 0 | -3.9% | -4.89 | -9.58 to -1.50 | +9.7% | 12.4% |
| 20 | Harvest the third of coins with the highest smoothed rate | -1.2% | -5.90 | -9.65 to -2.80 | +2.8% | 2.9% |
| | Spot, every coin (baseline) | +34.7% | +0.55 | -0.45 to +1.68 | +0.0% | 55.1% |
| 9 | Spot while the trend is above 0 | +26.5% | +0.67 | -0.44 to +1.78 | +0.6% | 35.5% |
| 11 | Spot while the trend is above 0 and the smoothed rate is at or under the median | +14.0% | +0.94 | -0.08 to +1.99 | +1.4% | 12.4% |
| 20 | Spot in the third of coins with the lowest smoothed rate | +14.0% | +0.79 | -0.13 to +1.86 | +2.0% | 21.0% |
| 16 | 60% trend sleeve, 40% harvest | +17.6% | +0.74 | -0.37 to +1.86 | | 21.5% |

850 days each. The harvest by year: 2024 +9.8%, 2025 +0.9%, 2026 -2.7% a year. Its daily returns correlate +0.12 with the trend sleeve's. The harvest's Sharpe is high because its only measured risk is the day-to-day wobble of funding; see Limits for what that leaves out.

### Does funding forecast spot?

Lowest fifth of coins by the signal minus the highest fifth, mean forward log return of spot:

| Signal | Horizon | Spread | 95% interval | Days |
|---|---|---|---|---|
| Smoothed rate | 7 days | +1.39% | +0.3% to +2.5% | 725 |
| Smoothed rate | 30 days | +3.36% | -1.0% to +8.0% | 702 |
| Smoothed rate, without KCS and ZEC | 7 days | +0.38% | -0.4% to +1.1% | 725 |
| Smoothed rate, without KCS and ZEC | 30 days | -0.14% | -3.0% to +3.2% | 702 |
| Smoothed rate against its own mean so far | 7 days | -0.23% | -1.3% to +0.9% | 666 |
| Smoothed rate against its own mean so far | 30 days | -3.55% | -8.8% to +0.9% | 643 |

The report also prints the forward return by quintile.

## Limits

- Basis is left out: the perpetual is assumed to track spot, so the harvest's only moves are funding and fees. In a spike the perpetual can trade well above spot and the short leg marks a loss until it converges, and at 1x margin a doubling of the price liquidates the short while the spot leg's gain sits on another venue. None of that is in the Sharpe above.
- Venue risk is left out: the harvest's whole capital sits on exchanges, half of it as margin.
- One venue's funding, 28 months of it, and one bull market (2024) in which it was rich. The yearly split is the finding; the full-window averages lean on 2024.
- The coins are today's kucoin whitelist, chosen with hindsight.
- Fees are a flat 0.1% per leg per side. Maker fees would cost less, which helps the switching rules most.
- The forecast, smoothing and fee settings are Carver's published ones or ours from the forecast family, with no tuning. Ten rules counted as trials; none clears its baseline.

## Run it

`python lab/futures/research/carry.py <userdir>` inside the image, with `DATABASE_URL` set for the funding (read-only is enough). It downloads kucoin daily candles into the userdir once, about a minute.
