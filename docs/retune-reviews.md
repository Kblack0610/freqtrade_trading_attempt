# Retune reviews

A retune suggestion (README, Suggestions) proposes saving the newest walk-forward fold's parameters as `<Strategy>.json` next to the strategy. Freqtrade loads that file on its own, so saving it changes every bot and backtest that runs the strategy. Each review below reads the fit before anyone saves it. One section per reviewed fit, newest first; a fit is applied only by a person.

Data: the `walkforward` and `suggestions` tables, read through `deploy/trader-web`.

## CMCWinner on binanceus, fit c0ead38f2247 (tr-381, 2026-11-23)

Proposed params: trained 2025-10-03 to 2026-04-01, tested April 2026. ROI 0.316 / 0.074 at 109 min / 0.033 at 273 min / 0 at 617 min, stoploss -0.106, no trailing, 3 open trades.

**Verdict: do not save.** The gates show that retuning each month helped this strategy on binanceus. They do not show that this one parameter set is good, and the bot keeps its current settings.

Gates (all pass, suggestion first seen 2026-11-19): tuned beat as-is in both of the newest two months, 1000 tuned trades over 13 months, gain per trade over as-is 90% low +0.11%, tuned expectancy 90% low +0.17%.

| Test month | In-sample trades | In-sample profit | As-is exp. | Tuned trades | Tuned exp. | Stoploss |
|---|---|---|---|---|---|---|
| 2025-04 | 87 | -7.9% | +0.26% | 100 | +0.97% | -0.330 |
| 2025-05 | 209 | +30.7% | -0.13% | 91 | +0.20% | -0.318 |
| 2025-06 | 299 | +59.7% | -0.14% | 100 | -0.03% | -0.294 |
| 2025-07 | 382 | +35.2% | +0.28% | 83 | +0.40% | -0.308 |
| 2025-08 | 434 | +51.1% | +0.11% | 74 | +0.64% | -0.264 |
| 2025-09 | 483 | +95.0% | -0.01% | 69 | +0.24% | -0.229 |
| 2025-10 | 460 | +120.0% | -0.01% | 68 | -0.04% | -0.273 |
| 2025-11 | 405 | +38.1% | -0.16% | 53 | -0.56% | -0.234 |
| 2025-12 | 445 | +13.0% | +0.31% | 82 | +0.49% | -0.090 |
| 2026-01 | 374 | +30.2% | -0.33% | 74 | +0.37% | -0.321 |
| 2026-02 | 384 | +17.2% | +0.32% | 74 | +0.56% | -0.317 |
| 2026-03 | 384 | -2.3% | +0.12% | 60 | +0.30% | -0.249 |
| 2026-04 | 424 | +2.8% | +0.18% | 72 | +0.66% | -0.106 |

Why not this fit:

- The parameters move every month. The first ROI step ranges from 0.129 to 0.383, and the proposed stoploss of -0.106 is an outlier: 11 of 13 folds chose -0.229 to -0.330. The evidence supports retuning monthly; one month's numbers are not stable enough to keep.
- Hyperopt found little in sample. The newest fold made +2.8% over 424 training trades (the folds before March 2026 mostly made +13% to +120%), so its +0.66% in April rests on one month and 72 trades.
- Tuning does not travel. Averaged over each venue's folds, tuned minus as-is per trade is +0.26% on binanceus (13 folds), +0.14% on okx (38) and -0.07% on kucoin (19).
- The strategy has open problems that come first: a `fix` suggestion since 2026-11-05 (no `startup_candle_count` declared, so the warm-up is unchecked) and a `demote` suggestion since 2026-11-21 for its binanceus bot (-55.01 over 60 days of dry run, 20 or more trades).

Revisit when the startup-candle fix has landed and a refit from that revision passes the gates again with a stoploss in the range the other folds chose.
