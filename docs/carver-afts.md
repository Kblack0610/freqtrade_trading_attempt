# Carver's Advanced Futures Trading Strategies, mapped to trader

Robert Carver, *Advanced Futures Trading Strategies* (Harriman House, 2023), builds 30 strategies on more than 100 futures, each adding one idea to the last, then four tactics. This page takes each one in the book's own order and names: the rule in our words, what it becomes on the spot crypto league, our version, and when we can use it. Status and the revisit trigger live in the [avenues register](avenues.md), one row per avenue, and this page names the row; a number for any strategy we can run comes from the nightly analysis on its strategy page, never from this page.

Sources: the table of contents on [Carver's book page](https://www.systematicmoney.org/advanced-futures-information); the rules of strategies 1 to 13 from public chapter notes ([majkong14/advanced-futures-notes](https://github.com/majkong14/advanced-futures-notes)); 14 to 30 from the table of contents and Carver's public writing, marked "confirm" until someone checks them against the book. The book's code and spreadsheets are for buyers only. We write our own versions in `lab/channels.py`; nothing is copied from pysystemtrade, which is GPL.

## Why the book's results do not transfer

- **One asset class.** The book's return per unit of risk comes largely from diversifying across bonds, equities, commodities and currencies. Coins move together: BTC takes out most of the shared variance (the residual clusters on /coverage), so the diversification multipliers that lift a futures portfolio are small here.
- **Long only.** The league runs spot only (`lab/league.py`). The book's directional strategies are long and short. A long-only port is a different strategy, an adaptation, and its results say nothing about Carver's.
- **Sized once.** Carver resizes every day as the forecast and volatility move. A port sizes its stake at entry, as the trend family does. Resizing an open trade needs position adjustment, which runs in backtest and paper but which `lab/live.py` refuses for live.
- **Short history.** Most coins have five to seven years of candles on our venues, against the book's 50.
- **Every port is a trial.** Each new class raises the deflated-Sharpe bar for all of them ([strategy coverage](strategy-coverage.md#every-added-strategy-raises-the-bar)).

## When we can use it

| Level | Meaning |
| now | Something we scan does this, a `Carver` class in the forecast family or an older one; read its strategy page |
| port | Runs long-only on spot; not built yet |
| port | Runs long-only on spot; still to build |
| research | Measurable offline from recorded data; cannot be traded by the league |
| needs shorts | The core of it is a short or a hedged leg: waits for a guarded short route (the US perps and CME rows in the register) |
| needs data | Needs more history than the venues have |
| needs scale | Only matters with real contracts, more coins or more capital than Stage 1 ($500, a $50 stake, three open trades: `[[book.stages]]` in `lab/suggest.toml`) |
| n/a | Futures plumbing with no spot equivalent |

The phases are in the trader plan for this work: P2 built the ports as the forecast family (`user_data/strategies/forecast/family.toml`, signals in `lab/forecasts.py`), P3 the carry study from recorded funding ([carry.md](carry.md)), P4 the long-short research.

## Part one: basic directional

| # | Strategy | Rule | On spot crypto | Ours | Use | Avenue |
|---|---|---|---|---|---|---|
| 1 | Buy and hold, single contract | Hold one contract. | Hold BTC. | The hold benchmark every strategy is scored against on /book | now | Core holding plus a capped active sleeve |
| 2 | Buy and hold with risk scaling | Size the holding so its annual risk matches a target, from a fixed volatility. | Spot can only size down: at crypto's volatility a 20 to 40% target keeps part in cash. It changes risk; it adds no edge. | `CarverVolHold` (1d only): a risk-scaled hold, beside plain holding | now | Volatility targeting |
| 3 | Buy and hold with variable risk scaling | As 2, with volatility from a recent EWMA blended 70/30 with its long-run average, so the size moves with it. | Resizing needs position adjustment (backtest and paper only). `lab/vol_target.py` already rescales walk-forward trades by forecast volatility. | `lab/vol_target.py` | research | Volatility targeting |
| 4 | Buy and hold portfolio with variable risk position sizing | 3 across many instruments, by weight, times an instrument diversification multiplier. | The multiplier is small for correlated coins, and Stage 1's three open trades limit the count. | `lab/mixtures.py` 1/N mixtures | research | Diversity archive and mixtures |
| 5 | Slow trend following, long only | Long while EWMAC(64,256) is above 0, sized as 4. | Runs as written. Nearest today: the Donchian ensemble and the MA crosses. | `CarverEwmac64` on the ladder, stake flat | now | Long-term trend filter |
| 6 | Slow trend following, long and short | As 5, short below 0. | The long half is 5; the short half needs a futures short. | P4 research on perpetual candles | needs shorts | Trend long-short on perpetuals |
| 7 | Slow trend following with trend strength | Forecast = EWMAC(64,256) over price volatility, scaled to an average absolute value of 10, capped at 20; position in proportion. | Long when above 0, stake by the forecast at entry. | `CarverTrendStrength64` on the ladder | now | Trend |
| 8 | Fast trend following, long and short with trend strength | 7 on EWMAC(16,64), with a buffer: trade only when the position leaves a band about 10% around its target. | Long half via the faster speeds; the buffer needs position adjustment; the short half needs shorts. Fees bind harder the faster it trades. | `CarverTrendStrength16` on the ladder, long half, no buffer; short half is P4 | now | Trend |
| 9 | Multiple trend following rules | Average several EWMAC(n,4n) speeds, each scaled and capped, times a forecast diversification multiplier, recapped at 20; drop speeds whose costs pass 0.15 Sharpe units. | The book's core trend system, portable. The cost cut maps to each venue's fees. Nearest today: TrendDonchian. | `CarverTrendMix` on the ladder: speeds 8 to 64, the two fastest of the book dropped for cost | now | Trend |
| 10 | Basic carry | Forecast from annualised carry over volatility, smoothed over 5 to 120 days, scaled and capped. | Crypto carry is the perpetual funding rate. Spot earns no funding, so it needs long spot plus a short perpetual. Recorded in `funding_rates`. | Measured in [carry.md](carry.md), no edge | research | Funding carry and basis |
| 11 | Combined carry and trend | Mix the trend and carry forecasts, about 60/40. | Needs 10. A long-only reading: trend entries filtered by funding. | Measured in [carry.md](carry.md), no edge | research | Funding carry and basis |

## Part two: advanced trend and carry

| # | Strategy | Rule | On spot crypto | Ours | Use | Avenue |
|---|---|---|---|---|---|---|
| 12 | Adjusted trend | Reshape forecasts where strength stops predicting: the fastest speed through a "double V" that falls to 0 beyond 20; two speeds scaled up and capped at 15. | A small effect per the notes; an option on 9 once 9 has results. | after P2 | port | Trend |
| 13 | Trend following and carry in different risk regimes | Multiply forecasts by 2 down to 0.5 as volatility rises against its 10-year average (quantile, smoothed). | Portable, with the quantile over all history so far rather than 10 years. | `CarverRegimeMix` on the ladder: the AFTS 9 mix times the multiplier, against the last 1000 candles | now | Volatility-regime scaling |
| 14 | Spot trend | Trend on the spot price rather than the back-adjusted futures price, which takes carry out of trend. Confirm. | Our prices are spot, so every trend strategy here is already a spot trend. | Every trend strategy | now | Trend |
| 15 | Accurate carry | A better carry estimate than two nearby contracts give. Confirm. | Funding is observed every 8 hours; the estimate is the smoothed recorded rate. | Measured in [carry.md](carry.md), no edge | research | Funding carry and basis |
| 16 | Trend and carry allocation | How to split risk between trend and carry by instrument or asset class. Confirm. | Needs 10 and 11. | Measured in [carry.md](carry.md), no edge | research | Funding carry and basis |
| 17 | Normalised trend | Trend on a price built from cumulative volatility-normalised returns, so instruments can be pooled. Confirm. | Changes little for one coin; it is the base for 18 and 19. | with 18 | port | Trend |
| 18 | Trend following asset classes | Trend on the average normalised price of the asset class, added to each instrument's trend. Confirm. | Crypto is one class, so this becomes a market-wide trend (BTC or an equal-weight basket) applied to every coin, through Freqtrade informative pairs. | later port | port | Long-term trend filter |
| 19 | Cross sectional momentum | Each instrument's trend against its class average: long the strongest, short the weakest. Confirm. | The long leg runs on spot. | XSMomentumRotation | now | Momentum |
| 20 | Cross sectional carry | Carry against the class average. Confirm. | Relative funding across coins. The long leg is measurable offline. | Measured in [carry.md](carry.md), no edge | research | Funding carry and basis |

## Part three: advanced directional

| # | Strategy | Rule | On spot crypto | Ours | Use | Avenue |
|---|---|---|---|---|---|---|
| 21 | Breakout | Where price sits in its rolling high-low range, smoothed, over windows from 10 to 320 days, as a scaled forecast. Confirm. | Portable. Our breakout family enters on a level break (ATR, Keltner, squeeze); this sizes by place in the range. | `CarverRange80` on the ladder | now | Breakout |
| 22 | Value | Slow reversal: long what has lagged its class over about five years, short what has led. Confirm. | Few coins have five years on our venues, dead coins are missing (survivor bias), and the short leg needs shorts. | none | needs data | Long-horizon value |
| 23 | Acceleration | The change in a trend forecast over its fast lookback: a trend speeding up. Confirm. | Portable. | `CarverAccel16` on the ladder (scalar approximate) | now | Acceleration |
| 24 | Skew, a case study | Recent return skew as a forecast: more negative skew has tended to earn a premium. Confirm. | The long leg ports: long when recent skew is negative. Crypto's skew flips with regimes. | `CarverNegSkew` on the ladder, long leg (scalar approximate) | now | Skew |
| 25 | Dynamic optimisation | For accounts too small for the full portfolio: choose whole-contract positions nearest the ideal fractional ones, greedily, with a cost penalty and buffer. | Spot sizes are near continuous (minimum orders of a few dollars), so the whole-contract problem barely exists at our size. | none | needs scale | Small-account position optimisation |

## Part four: fast directional

| # | Strategy | Rule | On spot crypto | Ours | Use | Avenue |
|---|---|---|---|---|---|---|
| 26 | Fast mean reversion | Fade the gap between the hourly price and a short daily equilibrium, scaled by volatility, with limit orders. Confirm. | Long-only dip buy on 1h or 15m. Fees decide it: Freqtrade fills a limit order on touch with no queue, so a limit-order result here flatters it; the port pays taker fees. Nearest today: the range family's z-score. | `CarverDipFast` on 1h and 15m only, taker fees (scalar approximate) | now | Mean reversion |
| 27 | Safer fast mean reversion | 26 with risk controls, standing aside when a strong trend runs against it. Confirm. | As 26 with a trend filter. | `CarverDipSafe` on 1h and 15m only: stands aside while EWMAC(16, 64) is under 0 | now | Mean reversion |

## Part five: relative value

| # | Strategy | Rule | On spot crypto | Ours | Use | Avenue |
|---|---|---|---|---|---|---|
| 28 | Cross instrument spreads | Trade the volatility-adjusted spread of two related futures back to its mean. Confirm. | Needs both legs. RatioRevert is the long leg alone, so it still carries the market. | RatioRevert (long leg) | needs shorts | Pairs, market neutral |
| 29 | Cross instrument triplets | As 28 across three instruments. Confirm. | Needs shorts and three legs at once. | none | needs shorts | Pairs, market neutral |
| 30 | Calendar trading strategies | Spreads between expiries of one future. Confirm. | Spot has no expiries. Dated crypto futures (CME, OKX) have a basis curve, but neither is a route we have. | none | n/a | Funding carry and basis |

## Part six: tactics

| Tactic | On spot crypto | Ours |
|---|---|---|
| One: contract selection and rolling | Spot does not roll; perpetuals have no expiry. | n/a |
| Two: execution | Paper fills, slippage and the decision record. | [live.md](live.md), `lab/decisions.py` |
| Three: cash and compounding | Stake sizing and the stage ladder. | `[[book.stages]]` in `lab/suggest.toml`, [operating rules](operating-rules.md) |
| Four: risk management | The kill switch, the daily-loss watch and the live stages' hard stop; a retirement needs a decay test (Carver's own view, cited in the lab `research.md`). | [operating rules](operating-rules.md) |

## What would move a strategy up a level

- **A guarded short route** (a verified US perpetual or CME micro route, as the register's route rows track) turns 6, the short half of 8, 10, 11, 20, 28 and 29 from research or blocked into something a paper league can trade.
- **Carry paying again.** [carry.md](carry.md) found the carry harvest paid in 2024 and not since, and no funding signal for a long-only account; 10, 11, 15, 16 and 20 are worth a short route only when a rerun shows it paying again.
- **Longer, survivor-free history** (dead coins included) opens 22.
- **Real contracts, or more coins than three open trades allow,** make 25 matter.
- **A port's own result.** Each `Carver` class is a counted trial. When its strategy page shows the confidence gates in [pipeline.md](pipeline.md) passing, it goes through promotion like any other strategy. When it fails them, the avenue row records the verdict.
