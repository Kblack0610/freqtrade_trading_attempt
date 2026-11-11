# The research pipeline, step by step

This guide walks through what happens between "a strategy file exists" and "the lab says whether it has earned a small real-money test", in plain words. The [README](../README.md) is the reference for every knob and table; each section here links to the part it summarises. Schedules, resources and environment values belong to home-config `apps/freqtrade/`, which wins wherever this page and it disagree.

## The question the pipeline answers

Every step serves one question: is there a strategy whose edge is real, survives fees, was not found by luck among everything tried, and behaves in a paper bot the way its backtest says? The answer is one of three things for each strategy and exchange: not enough evidence yet, evidence against, or ready for a small capped experiment. Nothing in the pipeline trades money; `lab/league.py` and `lab/live.py` refuse it ([the guard](../README.md#the-no-real-money-guard)).

The stop rule set before the 2026-11-07 run said: if no strategy is ready after that run, the recommendation is to hold BTC and end the live-money work. That was read on 2026-11-09 with no strategy ready, so the pipeline now runs as paper research.

## The whole flow

```
exchanges --backfill--> candle files --analysis--> backtest chunks --> windows and statistics
                                    \--walk-forward--> tuned months (out of sample) --> stress scenarios
windows + tuned months --> suggestions, confidence, book, mixtures --> research loop --> board rows and roster PRs
roster --> paper bots (the league) --ingest--> Postgres --> drift, watcher, decay test --> back into confidence
```

## The nightly clock

All times UTC. Every lab job runs on razer-laptop, because the candle files live on the `freqtrade-lab` volume there.

| When | Job | What it does | Typical length |
|---|---|---|---|
| every 5 min | `freqtrade-ingest` | Copies every paper bot's trades and a snapshot into Postgres | seconds |
| hourly at :20 | `freqtrade-backfill` | New candles for the league exchanges | about 2 minutes |
| hourly at :50 | `freqtrade-backfill-research` | New and older candles for okx and kucoin | about 1 minute once caught up |
| 01:00 | `freqtrade-walkforward` | Tunes and tests strategies month by month | up to the 4.5 h budget, 8 h hard limit |
| 02:45 | `freqtrade-analysis` | Backtests everything, then scores everything | under an hour on a normal night, hours after a big change |
| 05:35 | `freqtrade-futures` | Funding rates and open interest | under a minute |
| 10:00 | `trader-research-sync` (fleet machine) | Turns the night's changes into board rows and roster PRs | minutes |
| Sunday 02:50 | `freqtrade-lab-backup` | Tars the lab volume to the NAS | minutes |

## Step 1: price data

A backtest is only as good as its candles. A candle is one time bar of a pair: open, high, low, close and volume.

- `lab/backfill.py` keeps, per exchange, pair and timeframe (1m to 1d), every candle the exchange will give, prepended back to a floor per timeframe, and writes what it holds to `data_coverage`.
- Kraken's past comes from its published archive and its present from the newest candles each hour, because Kraken's API only serves recent candles.
- **The coverage guard.** An exchange and timeframe joins the analysis only when every pair on its whitelist is filled back to its oldest candle. A half-filled exchange would quietly compare strategies on different histories. Until then the run's `skipped` list says "not fully backfilled".
- **The pair list is survivor-only.** Whitelists are today's most traded pairs, so coins that died are missing from history. `/book` flags this per venue.

Reference: [Price data](../README.md#price-data), [Venues](../README.md#venues).

## Step 2: the nightly analysis (backtests)

A backtest replays a strategy over past candles as if it had traded, with the exchange's real fee plus a slippage guess.

- **Chunks.** Each strategy on each exchange is backtested one calendar year at a time, each year from the same 1000 USD wallet. A chunk is remembered by a fingerprint of everything that could change its result: the strategy file, the configs, the freqtrade version, the dates and the candles. A past year therefore runs once; a normal night only redoes the current year. Editing a strategy reruns all its years.
- **Bias checks.** Lookahead bias means the strategy peeked at the future; such a strategy ranks last and is never suggested. Recursive bias means an indicator still shifts at the strategy's startup candle count; that is a warning.
- **Windows.** The chunks are joined into one daily return series and measured over the last 30, 90, 180 and 365 days and the whole history.
- **Setup overlays.** The same strategy is also backtested with different account settings from `lab/variants.toml` (open-trade slots, stake, stoploss, fewer pairs). At most `[sweep] max_chunks` overlay chunks run a night; the rest wait, and the run's `overlay_chunks_waiting` counts them. Every overlay is a counted trial (see Step 5).

What the numbers mean:

| Number | Plain meaning |
|---|---|
| Sharpe interval low end | Return per unit of risk, on an unlucky draw (the bottom of a 90% range). The leaderboard ranks by this, so a strategy must look good even when luck goes against it |
| Edge before fees | Average profit per trade before fees. If the venue's fee is bigger than the low end of this, fees eat the edge |
| Over hold | Return beyond just holding the same share of capital in BTC. Near zero means the strategy only rode the market |
| Worst open | The deepest any trade went against its entry. Catches strategies that hide losers by never closing them |
| Deflated Sharpe | The chance this Sharpe beats the best one pure luck would produce across everything tried. Near 1 is evidence; a raw Sharpe of 1 is not |
| Overfit probability (PBO) | How often the best backtest on one half of the days lands in the bottom half on the other. Above 0.5 means picking the top backtest is worse than picking at random |
| Trades still needed | How many more trades the average needs before its low end clears zero, if it holds |
| Drift | For a strategy that is also a paper bot: how well the bot's trades match a backtest of the same days |

Reference: [The analysis](../README.md#the-analysis).

## Step 3: walk-forward (tuning tested on unseen months)

A backtest of a strategy as written says nothing about tuning it, and hyperopt (freqtrade's parameter tuner) always looks great on the data it tuned on. Walk-forward measures how much of that gain survives on data the tuner never saw.

- **A fold.** Take one whole month. Tune the strategy on the 180 days before it, stopping 7 days short so no trade spans the gap (the embargo). Then backtest the month as written and with the tuned settings. The tuned result on that month is out of sample: the tuner never saw it.
- **How many.** Up to `WF_LIMIT` candidate strategies, on each walk-forward exchange (binanceus, okx, kucoin), for each of the last `WF_FOLDS` (12) months before the lockbox. Coinbase is left out because its fees leave nothing to tune.
- **Budget and carry-over.** After `WF_BUDGET_S` (4.5 h) a run starts no new fold, records the run with `folds_left`, and the next night picks up where it stopped. A change that touches every fold's fingerprint (a new freqtrade version, new stress scenarios) puts every fold back in the queue, which takes several nights to drain.
- **Stress scenarios.** Each tuned month is replayed with entries one candle late, each numeric parameter nudged 10% up and down, and fills checked on 1m candles. A strategy whose edge vanishes under any of these is fragile.
- **Reading it.** Walk-forward efficiency is tuned return out of sample over tuned return in sample. Near 1, the tuning carries over; near 0 or below, the tuner fitted noise.

Reference: [Walk-forward](../README.md#walk-forward), [Stress gates](../README.md#stress-gates).

## Step 4: the reproduce check

The cache is only safe if the same inputs give the same outputs. Each night `lab/reproduce.py` reruns a few cached chunks and one fold at random and compares them with what is stored; any difference alerts `TraderReproMismatch`. Reference: [Reproduce](../README.md#reproduce).

## Step 5: scoring and decisions

At the end of the analysis the derived tables are rebuilt from everything stored.

- **Suggestions** (`lab/suggest.py`): promote to the paper league, watch, demote, retire, fix or retune, each showing every gate it was held to. Promote decides who enters the paper league; it is the looser bar.
- **Confidence** (`lab/confidence.py`): a score from 0 to 100, the chance that the true profit per trade after fees is above zero, from the tuned out-of-sample trades, pulled toward the average of every strategy tried. No score exists until at least 20 strategies have 30 out-of-sample trades each.
- **Ready** means the score is at least 90 and every gate passes. It earns a small capped experiment and nothing more.
- **The trial register.** `trials` counts every strategy, exchange, overlay, tuned setting and mixture ever tried, and never forgets one. The more things are tried, the higher the bar for all of them, because more tries make a lucky winner more likely.
- **The lockbox.** Months from `[lockbox] start` (2026-05-01) on are kept out of tuning and scoring. When a strategy clears everything else, a PR moves the date and it gets one look at those months.
- **Book and mixtures** (`lab/book.py`, `lab/mixtures.py`): what a scored strategy might return over 1 and 5 years next to holding BTC, and portfolios of several strategies fitted on earlier months and tested on the next.

The confidence gates, from `lab/suggest.toml` `[confidence]`, and what each one waits on:

| Gate | Needs | Waits on |
|---|---|---|
| bias | lookahead and recursive clean | compute (nightly) |
| costs | per-trade fee matches the venue's real fee | compute |
| out-of-sample trades | at least 250 tuned out-of-sample trades | more walk-forward months, so time |
| expectancy interval low | 95% interval of profit per trade above zero | trades, so time |
| deflated Sharpe | at least 0.95 against the whole trial register | data length, and fewer trials |
| overfit probability | at most 0.25 on the venue | data length |
| minimum backtest length | out-of-sample years of at least 2 ln(N) / Sharpe^2, N the trial count | time: with about 1,100 trials, a Sharpe of 1 needs about 14 years, 2 needs about 3.5, 3 needs about 1.6 |
| months positive | 70% of at least 6 out-of-sample months | months, so time |
| regime spread | profitable across BTC up, down and flat climates | the market visiting each climate |
| excess over hold | beats holding BTC at the low end | data |
| worst open loss | no trade deeper than 25% against entry | compute |
| cost stress | still positive with half the gross edge and 0.1 to 0.4% extra cost per round trip | compute |
| stress scenarios | survives late entry, nudged parameters, 1m fills | compute |
| venues agreeing | positive on at least 2 exchanges | compute and data |
| drift | the gap sequence (paper minus backtest, per matched trade) sits inside the expectancy interval half width, anytime-valid at alpha 0.05 split over the live bots; fails early when it lies outside | paper bot running, so time |
| dry run | the paper expectancy sequence low end above zero (anytime-valid, [design](anytime-gates.md)), after 30 days, through a BTC fall of 10% or more, with at least one real stop; fails early when its high end drops below zero | paper trades, and the market for the fall |
| not decayed | the decay test has not fired on paper or live trades | time |
| score | at least 90 | the above |

Reference: [Suggestions](../README.md#suggestions), [Confidence](../README.md#confidence), [Book](../README.md#book).

## Step 6: the research loop and sync

`lab/loop.py`, the last step of the analysis, compares tonight's passing strategies, promote suggestions and retune fits with last night's and writes what changed as `research_events`. At 10:00 UTC `ci/research_sync.py` on the fleet machine files one board row a night for the changes and opens a home-config roster PR for each promotion. A person merges it; nothing promotes itself. Reference: [Nightly research loop](../README.md#nightly-research-loop).

## Step 7: the paper league

Each row of home-config `apps/freqtrade/roster.tsv` is one paper bot: a strategy on an exchange with an optional overlay, grouped into cohorts (`strategies`, `aggressive`, `mixture`, `markets`). The league is where the time-based gates fill up.

- `freqtrade-ingest` copies every bot's trades into Postgres every 5 minutes.
- Drift compares each bot's trades with a backtest of the same days. A low match rate means the backtest does not model what the bot does; a steady negative gap means fills or fees the backtest leaves out.
- `lab/watcher.py` pauses a bot that falls past its hard stop.
- The decay test (a CUSUM over closed trades) says when trades stop looking like what walk-forward measured, and suggests retiring the strategy.

Reference: [Cohorts](../README.md#cohorts), [Watcher](../README.md#watcher).

## Adding a strategy: what happens, and when

| When | What happens |
|---|---|
| Minutes | Drop the file in `user_data/strategies/`, add its class to `lab/families.toml`, check it loads (see [Adding a strategy](../README.md#adding-a-strategy)), merge. The image bump deploys it |
| The next night | Analysis backtests its whole history on every exchange and runs the bias checks. It appears on the leaderboard |
| When chosen | It becomes a walk-forward candidate only when it is in the league, suggested, or among the best on a venue. Then each exchange needs its 12 months of folds, spread over nights by the budget |
| Weeks to months | It needs 250 tuned out-of-sample trades, 6 or more months mostly positive, and enough out-of-sample years for its Sharpe |
| 30 days or more after it starts paper trading | The dry-run gate, and only if BTC fell 10% or more in that time and a stop fired |

Writing the strategy is minutes; reaching a verdict is mostly waiting for months of evidence.

## Where the time goes

Two kinds of time limit the pipeline, and they need different fixes.

- **Compute time** is machine hours: walk-forward folds, overlay chunks, backfill. More or faster machines and less redundant work shorten it.
- **Calendar time** is evidence that only arrives as days pass: new out-of-sample months, paper trades, a BTC fall during the dry run. More machines do not shorten it; only a design that asks for less of it, or finds the same evidence in history, does.

To see the compute backlog, read the newest run of each kind:

```sh
kubectl --context home-k3s -n freqtrade exec deploy/trader-web -c web -- python -c "
import os, psycopg
c = psycopg.connect(os.environ['DATABASE_URL'])
for kind in ('analysis', 'walkforward'):
    r = c.execute('select started_at, finished_at, raw from runs where kind=%s and raw <> %s::jsonb order by id desc limit 1', (kind, '{}')).fetchone()
    keys = ('chunks_run', 'chunks_cached', 'overlay_chunks_run', 'overlay_chunks_waiting', 'folds_run', 'folds_left', 'trial_register', 'confidence_ready')
    print(kind, r[1] - r[0], {k: r[2][k] for k in keys if k in r[2]}, 'skipped', len(r[2].get('skipped', [])))
"
```

| Field | Read it as |
|---|---|
| `chunks_run`, `chunks_cached` | Backtests done tonight against those reused. Many runs on a quiet night mean something changed every fingerprint |
| `overlay_chunks_waiting` | Overlay backlog. Divide by `[sweep] max_chunks` for the nights it takes to drain |
| `folds_run`, `folds_left` | Walk-forward done and still queued. `folds_left` should fall night over night |
| `skipped` | Strategies and venues left out, with the reason (usually "not fully backfilled") |
| `trial_register` | Things tried so far. It only grows, and it raises the deflated Sharpe and minimum backtest length bars |
| `confidence_ready` | Strategies ready for a capped experiment |

The `/confidence` page shows each strategy's gates and which ones it fails.

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| A strategy is missing from the leaderboard | It is in the run's `skipped` list. "Not fully backfilled" clears when backfill reaches the oldest candle; a load error needs the strategy fixed |
| `folds_left` stays high for days | A fingerprint change requeued every fold. It drains at roughly one budget per night; check `folds_run` is not zero and the job is not hitting its deadline |
| Analysis takes hours on a quiet night | Something changed every chunk's fingerprint: a strategy edit, a config or fee change, or a freqtrade bump |
| The score is high but the strategy is not ready | Ready needs every gate. Time-based gates (minimum backtest length, the dry run floor of 30 days, a BTC fall and a stop) cannot pass early; the drift and dry run sequences pass once enough paper trades say so |
| `folds_run` is 0 and the night's walk-forward or analysis never started | The pod is Pending. `kubectl -n freqtrade describe pod` says `Insufficient cpu` when the job requests more than razer-laptop has free (it offers pods 4 CPUs); TraderLabPodUnschedulable fires after 30 minutes. The fix and the row for it are in home-config `apps/freqtrade/README.md` |
| Every confidence row fails dry run and drift | Those gates need the strategy as a paper bot with enough closed trades for the sequences (a 0.3 SD edge takes about 130; drift rows written before migration 040 carry no gaps until the next analysis run); check it is in the roster |
