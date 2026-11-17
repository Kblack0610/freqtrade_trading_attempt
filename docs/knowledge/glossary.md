# Trading terms

The market vocabulary a person needs to read this repo, each with what it means and where the trader uses it. Words the site itself defines (paper, league, account, cohort, lane, decision, the scorecard columns) live in `web/glossary.toml` and render on `/guide#glossary`; this page does not repeat them.

Checked against the code at master b9907bd, 2026-11-17.

## Lot size

The unit a venue makes you trade in. A venue sets two rules: a step, which every quantity must be a multiple of, and a minimum order. An order between steps is rounded down; an order under the minimum is refused.

| Market | Lot | Example |
| --- | --- | --- |
| Forex | Standard lot 100,000 units of the base currency; mini 10,000; micro 1,000 | 1 standard lot of EUR/USD is about $100k of exposure |
| Stocks | 1 share; some brokers sell fractions | 10 shares; 10.4 needs a broker that sells fractions |
| Crypto | Per pair: an amount step (precision) and a minimum amount and cost | A BTC/USD step of 0.00001 BTC with a minimum of a few dollars |
| Event contracts (Kalshi) | 1 contract, which pays $1 if it resolves your way | $10 at 40c buys 25 contracts |

Why it matters: rounding leaves part of a small stake unused, and an order sized under the minimum never reaches the book.

How the trader handles it:
- Crypto league (Freqtrade): Freqtrade rounds every order to the pair's step (`Exchange.amount_to_precision` in its source) and computes a minimum stake per pair from the exchange's limits, plus `amount_reserve_percent` (default 5%) and the stoploss, refusing a stake below it ([configuration docs](https://www.freqtrade.io/en/stable/configuration/)). The repo has no lot-size code of its own. Stakes are fixed per stage, $50, $100 and $200 (`lab/suggest.toml` `[[book.stages]]`), well above the usual minimums on the venues in `user_data/exchanges/`.
- Event contracts: `lab/markets/backtest.py` `take` buys `floor(stake / price)` whole contracts, capped by the book depth it may take. The paper league fills through the same function, so paper and backtest round the same way.
- Alpaca paper (`lab/broker.py`): whole shares, `qty` an integer.

## Sizing

| Term | What it means | In the trader |
| --- | --- | --- |
| Position sizing | How much goes on each trade. It decides survival more than entries do | Fixed stake per stage, [live.md](../live.md) |
| Stake | The money put into one position | `stake_amount`; `unlimited` is refused for live |
| Risk per trade | What you lose if the stop is hit: stake times the distance to the stop. A $50 stake with a 10% stop risks $5 | Freqtrade `stoploss` per strategy |
| Kelly criterion | The fraction of capital that maximizes long-run growth for a known edge: for a bet paying b to 1 that wins with probability p, f = p - (1 - p) / b | Not used: [avenues.md](../avenues.md) row "Kelly or point-estimate stake growth" is NO-GO |
| Fractional Kelly | Betting a fraction c of Kelly keeps c(2 - c) of the growth: half Kelly keeps 75% with far less drawdown; double Kelly grows at zero (Thorp 2006) | Why fixed steps from the edge's lower bound win over Kelly while the edge is an estimate |
| Volatility targeting | Sizing each position so it carries the same expected risk, smaller in wild markets and larger in calm ones (Carver) | Research only: `lab/vol_target.py` |
| Leverage | Exposure larger than capital, borrowed. It multiplies losses as much as gains | None: `position_adjustment_enable` is false and nothing borrows |
| Correlation in a crash | Assets that look independent fall together in a crash | Plan as if every crypto bot stops out at once (lab `research.md`, section 3) |

## Costs and fills

| Term | What it means | In the trader |
| --- | --- | --- |
| Bid, ask, spread | The best buy price, the best sell price, and the gap between them. Crossing the spread costs half of it each way | Event-contract fills buy at the ask (YES) or one minus the bid (NO) |
| Slippage | Filling at a worse price than intended, from latency or thin books | Freqtrade backtests assume none, so live runs worse; [pipeline.md](../pipeline.md) cost gates |
| Maker and taker | A maker rests an order on the book; a taker crosses the spread to fill now. Takers pay more fee and give up the spread | Our fill models are taker; Kalshi maker orders are a blocked avenue |
| Fee | The venue's charge per order. Kalshi's taker fee is `ceil(0.07 * C * P * (1 - P))` | [markets.md](../markets.md) Fees |
| Liquidity and depth | How much can trade near the current price before the price moves | `depth_share` (0.5, `lab/markets.toml`) caps how much of the top of the book a fill may take |
| Limit and market order | A limit order names its worst price; a market order takes whatever the book gives | The Alpaca and Kalshi drills rest limit orders far from the market |
| Stop loss | An exit when price falls a set distance. It is a survival rule; one stop-out says nothing about whether a strategy broke | A live position keeps its stop on the exchange, `stoploss_on_exchange` ([live.md](../live.md)) |

## Measuring results

| Term | What it means | In the trader |
| --- | --- | --- |
| Edge, expectancy | Average profit per trade, or per dollar, after every cost. Without a positive one nothing else matters | `expectancy_level` interval in the confidence gate |
| Win rate and payoff | How often you win, and the average win over the average loss. Neither means anything without the other | Freqtrade's `winrate`, read by `lab/ingest.py` |
| Drawdown | The fall from a peak. Recovery from a max drawdown takes about three times as long as the fall under normal returns (Bailey and Lopez de Prado) | Max drawdown column, `/guide#glossary` |
| Sharpe ratio | Mean return over its standard deviation, usually annualized. A rough return per unit of risk | `lab/metrics.py`, annualized with sqrt(365) because crypto trades every day |
| Deflated Sharpe | The chance a Sharpe beats the best one pure luck would give across everything tried | [pipeline.md](../pipeline.md) Step 2 and the trial register |
| Probability of backtest overfitting (PBO) | How often the best backtest on one half of the data lands in the bottom half on the other | [pipeline.md](../pipeline.md) Step 2 |
| Closing line value (CLV) | Whether you bought at a better price than the final one before the event. The betting world's standard early proof of skill, read long before settled profit says anything | `close_mid` recorded per open event-contract fill, [markets.md](../markets.md) |
| Calibration | Whether things priced at 30c happen about 30% of the time | The `calibration` family |

## Research traps

| Term | What it means | In the trader |
| --- | --- | --- |
| Overfitting | Tuning a strategy to past noise; it shines in the backtest and fails live | Trial register, deflated Sharpe, PBO |
| Lookahead bias | A backtest using data it could not have had at the time | `freqtrade lookahead-analysis` |
| Recursive or startup bias | Indicators that change with how much history they were computed from | `freqtrade recursive-analysis` |
| Survivorship bias | Testing only on assets that still exist, which hides the ones that died | Lab `research.md`, section 2 |
| Data snooping or multiple testing | Trying many variants and keeping the best; the best of many is lucky by construction | Every variant counts as a trial |
| Walk-forward, out-of-sample | Tune on one period, test on a later one the tuning never saw | [pipeline.md](../pipeline.md) Step 3 |
| Edge decay | A published or discovered edge shrinks once others trade it | Public-strategy selection is NO-GO in [avenues.md](../avenues.md) |
| Wash trading | Fake volume an exchange trades with itself; over 70% on unregulated exchanges in one study | Lab `research.md`, section 2 |

## Betting and event contracts

| Term | What it means | In the trader |
| --- | --- | --- |
| Event contract | A contract paying $1 if an event happens and $0 if not, so its price reads as a probability | [markets.md](../markets.md) |
| Implied probability | The probability a price or odds stand for: 40c is 40%; decimal odds 2.50 are 1 / 2.50 = 40% | `lab/markets/families.py` turns each sportsbook's odds into it |
| Vig, overround | The bookmaker's margin: the implied probabilities of every outcome add to more than 100% | Each book's implied odds are divided by their sum before a comparison |
| Devig | Removing the margin to get fair probabilities | The median devigged book is the `gap` family's fair price |
| Favorite-longshot bias | Cheap longshots are overpriced and favourites slightly underpriced | The `longshot` family; [prediction-markets.md](prediction-markets.md) |
| Sharp and square | Bettors who beat the closing line, and those who do not. Pinnacle's closing line is the usual sharp reference | [prediction-markets.md](prediction-markets.md) |
