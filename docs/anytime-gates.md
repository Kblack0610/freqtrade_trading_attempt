# Anytime-valid paper gates (proposed design)

Status: proposed 2026-11-11. Ask K21YCW was answered "a" by the fleet judge the same day, and the human can override it. Nothing in `lab/` or `lab/suggest.toml` has changed yet: the build is its own queued item. This is phase 8 of the plan `magical-stargazing-owl`.

## What it would replace

Two gates in `lab/confidence.py` judge a candidate by its paper (dry-run) bot:

| Gate | Today (`[confidence]` in `lab/suggest.toml`) | What it costs |
|---|---|---|
| drift | at least `min_drift_trades` = 20 matched trades, and the mean paper-minus-backtest gap inside the half width of the out-of-sample expectancy interval | about 20 trades, then a pass or fail on one fixed-sample comparison |
| dry run | `min_live_days` = 90, `min_live_trades` = 60, BTC down `min_live_btc_drop` = 10% during the run, `min_live_stopouts` = 1 | at least 90 days for every candidate, strong or broken |

`decay` (a CUSUM against the out-of-sample low end) already reads trades one at a time and stays as it is.

Two problems with the fixed counts:

- A strong candidate waits the full 90 days, and a broken one runs the full 90 days before anything says so.
- The drift gate is re-read every night. A fixed-sample test checked every night until it passes has a false pass rate well above its nominal level (optional stopping). The gate is not wrong yet only because 20 trades acts as a floor rather than a stopping rule.

## The idea

A confidence sequence is a confidence interval that holds at every trade count at once: the chance it ever excludes the true value, over the whole run, is at most alpha (Ville's inequality). So the nightly job may read it every night and stop as soon as it says enough, with no penalty for looking.

Two sequences per candidate x venue, both updated on each closed paper trade:

1. Paper expectancy: the mean net return per closed paper trade.
   - Pass when the lower bound is above zero.
   - Fail early when the upper bound is below zero.
   - Method: the betting confidence sequence for a bounded mean (Waudby-Smith and Ramdas, JRSS B 2024). A trade's return is bounded below by the stop and above by the ROI table, so the bounds are known per strategy. It is nonparametric and tighter than the sub-Gaussian bounds for skewed trade returns.
2. Drift: the mean gap per matched trade, paper minus backtest of the same days (`metrics.drift`, already computed).
   - Pass when the whole sequence sits inside plus or minus the expectancy interval's half width: the gap is small enough to ignore (an equivalence test, the same band the drift gate uses today).
   - Fail early when the sequence lies entirely outside that band.
   - Method: the empirical-Bernstein stitched boundary (Howard, Ramdas, McAuliffe and Sekhon, Annals of Statistics 2021). Gaps are not bounded as tightly as returns, and it adapts to their variance.

Several candidates run at once, so alpha is split across the paper league: either Bonferroni over the bots live at the time, or e-BH (Wang and Ramdas 2022) over their e-processes. Both keep the league-wide false pass rate at alpha whenever any of them is read.

## What it does to calendar time

Trades needed before the expectancy sequence can pass, two-sided, alpha 0.05, normal-mixture boundary tuned near 100 trades (a rough guide; the betting sequence is a little tighter on bounded data):

| Edge per trade, in standard deviations | Anytime-valid | Fixed z test at one look |
|---|---|---|
| 0.5 | 66 | 16 |
| 0.3 | 134 | 43 |
| 0.2 | 253 | 97 |
| 0.1 | 922 | 385 |

Read this honestly:

- The expectancy sequence is a stricter test than anything the dry run gate checks today. The dry run gate only checks behaviour (time, trades, a fall, a stop) and never asks whether paper made money. For a thin edge it would take more than 60 trades.
- The time it saves is elsewhere. A broken candidate fails as soon as its upper bound drops below zero, often in days. A candidate whose paper fills match the backtest passes drift without waiting for a fixed count. And a strong edge (0.3 SD or more a trade) can pass in about the 60 trades the dry run already asks for, without the 90-day clock.
- A strategy that trades rarely cannot reach any of these counts quickly. Calendar time for it stays where it is.

## What it does not replace

The BTC fall and the real stop-out test behaviour rather than edge: has the bot ever been through a drawdown in the market, and does a stop on the exchange actually fire. No statistic over trades says that. The options below differ on whether they stay as a floor.

## The stop rule

The thresholds are fixed before any paper result is read, and no result moves them afterwards. That is why this page writes the design and changes nothing: alpha, the floors and the bands are the human's to set once, now, and the code then reads them from `lab/suggest.toml` like every other gate.

## The ask

Filed as K21YCW on 2026-11-11 (task tr-176); answered a.

- a (recommended): both sequences replace the fixed counts: drift loses `min_drift_trades`, dry run loses `min_live_days` and `min_live_trades` and gains the expectancy sequence. A floor stays: 30 days, BTC down 10% during the run and one real stop-out. Alpha 0.05 league-wide, Bonferroni over live bots. Strong candidates can pass early, broken ones fail early, and the behaviour checks a backtest cannot give remain.
- b: early fail only. The sequences can only fail a candidate; passing still needs today's 90 days, 60 trades, fall and stop. Safest, and it saves no calendar time for a good candidate.
- c: sequences only, no floor. Fastest, but a candidate could pass without ever living through a fall or firing a stop.

## The build

One PR in this repo:

- `lab/metrics.py`: the two confidence sequences, numpy, each with a coverage test that simulates optional stopping (the negative control: a fixed-sample t test read every night breaks its alpha; the sequence keeps it).
- `lab/suggest.toml` `[confidence]`: the alpha and the floors the answer names; removed keys go in the same PR.
- `lab/confidence.py`: the drift and dry run gates read the sequences.
- `docs/pipeline.md`: the gate table rows for drift and dry run.

No cache key changes and no recompute: the sequences read paper trades and drift rows that ingest already stores.
