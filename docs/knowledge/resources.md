# Free resources

Free material worth the time, by subject, each with what it covers and why it matters here. Links were fetched live on 2026-11-17 unless marked snippet.

## Freqtrade itself

| Resource | What it gives | Why it matters |
| --- | --- | --- |
| [Backtesting](https://www.freqtrade.io/en/stable/backtesting/), section "Assumptions made by backtesting" | Fills at the candle open with no slippage; a stoploss exits at the stop price plus 2x fees; order of checks is exit signal, stoploss, ROI, trailing stop | Says why a Freqtrade backtest runs better than live. `--timeframe-detail` reduces the intra-candle error |
| [Lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/) | Reruns with shifted indicators to find a strategy reading the future | The first check on any new strategy |
| [Recursive analysis](https://www.freqtrade.io/en/stable/recursive-analysis/) | Finds indicators that change with how many candles they start from | Only compares the last row; it does not show whether entries move |
| [Hyperopt](https://www.freqtrade.io/en/stable/hyperopt/) | The optimiser and its loss functions | Its overfitting advice is one line; walk-forward is ours ([pipeline.md](../pipeline.md)) |
| [Configuration](https://www.freqtrade.io/en/stable/configuration/) | Stake, minimum stake and `amount_reserve_percent` | Lot-size handling, [glossary.md](glossary.md#lot-size) |

## Overfitting and research method

| Resource | What it gives | Why it matters |
| --- | --- | --- |
| Bailey, Borwein, Lopez de Prado, Zhu, [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) (2015) | PBO and the CSCV method | The PBO gate in [pipeline.md](../pipeline.md) |
| Bailey, Lopez de Prado, [The Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) (2014) | Sharpe corrected for the number of trials and fat tails | The deflated Sharpe gate and the trial register |
| Bailey, Borwein, Lopez de Prado, Zhu, "Pseudo-Mathematics and Financial Charlatanism", Notices of the AMS 61(5), 2014 ([copy](https://carmamaths.org/resources/jon/backtest.pdf), snippet) | With five years of data, about 45 configurations can already show a Sharpe of 1 with no skill | The shortest case for counting every trial |
| [QuantConnect Research Guide](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide) | Short notes on overfitting, lookahead, survivorship and outliers | A one-page checklist |

## Systematic trading

| Resource | What it gives | Why it matters |
| --- | --- | --- |
| [Robert Carver's blog](https://qoppac.blogspot.com/p/systematic-trading-start-here.html) | Start-here index; posts on overfitting ("The three kinds of overfitting", 2015), random-data tests, how fast to trade given costs. Active, last post 2026-11-16 | The free companion to *Systematic Trading* |
| [pysystemtrade](https://github.com/pst-group/pysystemtrade) | Carver's framework in Python, GPL-3.0. Moved from robcarver17 in January 2026 | Reference code for vol targeting and forecast scaling; futures-centred |
| [Quantopian lectures](https://github.com/quantopian/research_public) | The old Quantopian lecture notebooks; videos at the Internet Archive `quantopian-archive` collection (snippet) | Good statistics lectures; notebooks need edits to run, as Quantopian's data calls are gone |
| [MIT OCW 18.642](https://ocw.mit.edu/courses/18-642-topics-in-mathematics-with-applications-in-finance-fall-2024/) (Fall 2024) | Probability, statistics and portfolio theory for finance, with videos | The maths underneath; heavy |

## Sizing

| Resource | What it gives | Why it matters |
| --- | --- | --- |
| Thorp, [The Kelly criterion in blackjack, sports betting and the stock market](https://gwern.net/doc/statistics/decision/2006-thorp.pdf) (2006) | Kelly derived, fractional Kelly, what an overestimated edge does | Why [avenues.md](../avenues.md) rejects Kelly sizing for now; checked in lab `research.md`, section 3 |
| MacLean, Thorp, Ziemba, "Good and bad properties of the Kelly criterion" (Quantitative Finance, 2010; snippet) | Full Kelly is very sensitive to errors in the mean | The same lesson from the source |

## Podcasts

- [Flirting with Models](https://www.flirtingwithmodels.com/) (Corey Hoffstein): quant practitioners on method.
- [Top Traders Unplugged](https://www.toptradersunplugged.com/), its Systematic Investor series: trend following and systematic funds; Carver appears often (cited in lab `research.md`, section 1).
- [Chat With Traders](https://chatwithtraders.com/), the Quant, Algo, Systematic category.

## Event contracts and betting

In [prediction-markets.md](prediction-markets.md), with the evidence.

## Skipped

- Hudson & Thames: mostly paid now; mlfinlab moved from BSD to a proprietary licence in 2021. The free papers above cover its ideas.
- r/algotrading wiki: could not be fetched, so not reviewed.
