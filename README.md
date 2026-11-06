# freqtrade_trading_attempt

Strategy code and tooling for a Freqtrade dry-run league: a set of paper-trading bots, each running one strategy on one exchange, plus a nightly analysis that backtests every strategy over all the price history there is and ranks them with confidence intervals, bias checks and return estimates. No real money, ever.

The league runs on home-k3s. Its deploy (roster, StatefulSet, CronJobs, secrets) lives in home-config at `apps/freqtrade/`. Every pod runs one image, `ghcr.io/kblack0610/trader`: the pinned `freqtradeorg/freqtrade:2026.9` release plus psycopg plus this repo at `/repo`. GitHub Actions builds it on each push to master (tags `sha-<7>` and `latest`), and home-config pins it by digest, so a change here goes live only when home-config bumps the digest.

Results go to the `trader` database in the cluster's shared Postgres, which the nightly `pg_dumpall` backs up. The site in `web/` shows them, behind a Forgejo sign-in; home-config deploys it.

## Layout

| Path | What it is |
|---|---|
| `user_data/strategies/` | Strategies, found with `--recursive-strategy-search` |
| `lab/families/` | Staged strategy families, not yet scanned: candles, chart and the FreqAI `ml/` (see Staged families) |
| `lab/patterns.py` | Causal chart-pattern signals: swing pivots, support and resistance breaks, double tops and bottoms, triangles, flags |
| `user_data/config.base.json` | Shared config: dry run, 1000 USD wallet, spot, static pair list |
| `user_data/exchanges/<name>.json` | Per-exchange overlay: name, pair whitelist, pricing quirks; no keys |
| `lab/league.py` | Bot entrypoint: picks its roster row from the pod ordinal, refuses anything but key-less spot dry run, writes the API config, execs `freqtrade trade` |
| `lab/pairs.py` | By hand: a research venue's whitelist, its most traded pairs with history since 2022 |
| `lab/futures.py` | Daily job: funding rates and open interest for the okx and kucoin swaps into `funding_rates` and `open_interest` (see Price data) |
| `lab/markets/`, `lab/markets.toml` | Event contracts, kept out of the Freqtrade runtime: the venue adapter interface (`venue.py`), read-only adapters for Kalshi, Polymarket US and global, PredictIt, Manifold and The Odds API (sportsbook odds), the cross-venue linker (`links.py`: rules, LLM proposals, confirmation by resolution), and the recorder, a 5-min job writing change-only quotes to MinIO parquet and market metadata to Postgres (see `docs/markets.md`, Recorder) |
| `lab/backfill.py` | Hourly job: price history for every exchange x pair x timeframe into `data/<exchange>/` on the lab PVC, and what it holds into `data_coverage` (see Price data) |
| `lab/analyze.py` | Nightly job: every strategy x exchange backtested in calendar-year chunks over the whole history, lookahead and recursive bias checks, then the derived tables (see The analysis) |
| `lab/walkforward.py` | Weekly job: hyperopt on the months before each of the last whole months, then that unseen month backtested as is and tuned (see Walk-forward) |
| `lab/metrics.py` | Pure functions behind the derived tables: daily returns, Sharpe, Sortino, drawdown, block-bootstrap intervals, deflated Sharpe with effective trials, overfit probability (CSCV), trades still needed, Monte Carlo, BTC regimes |
| `lab/suggest.py`, `lab/suggest.toml` | After each analysis and walk-forward: promote, watch, demote, retire, fix and retune suggestions from the derived tables, with the thresholds as data (see Suggestions) |
| `lab/confidence.py` | After each analysis and walk-forward: the score and gates that say whether a strategy has earned real money, and the trial register (see Confidence) |
| `lab/families.toml` | By hand: each strategy class's method family (trend, mean-reversion, momentum and the rest) and data inputs, for `/coverage` |
| `lab/book.py` | After the score: what each scored strategy might return beside holding BTC, and what each stage costs and can lose. |
| `lab/live.py` | Live bot entry point: always refuses, nothing deploys it. See `docs/live.md` |
| `lab/ingest.py` | Hourly job: every bot's trades (closed and open) and a snapshot (profit, balance, dry-run flag) into Postgres |
| `lab/watcher.py` | Daily loss watch: each bot's marked-to-market equity every 60 s into `watch`, and the kill drill. See Watcher |
| `lab/migrate.py`, `lab/migrations/` | Schema: numbered SQL files applied once each, in order, under an advisory lock; runs as the init container of every job |
| `lab/db.py` | The one Postgres connection: `DATABASE_URL` |
| `web/` | The site: FastAPI and Jinja, read-only, Forgejo sign-in (`web/auth.py`), server-drawn SVG charts (`web/charts.py`); pages Overview, bot detail, Strategies, strategy detail, Walk-forward, Suggestions, Confidence, Book, Coverage, Guide, Data, Runs; `/guide` is the reading order (see The site) |
| `lab/tests/` | pytest; the DB tests need `DATABASE_URL` pointing at a throwaway database |
| `Dockerfile`, `lab/requirements.txt` | The `trader` image |
| `.github/workflows/image.yml` | CI: build, migrate twice, tests, every strategy loads; on master also push to GHCR |

## The no-real-money guard

`lab/league.py` exits 2 before freqtrade starts if any `FREQTRADE__` env var is set, if the merged config is not `dry_run: true`, if it is not spot, or if the exchange block carries a key, secret, password, uid, private key or wallet address. Public market data needs no keys, so the exchange files keep them empty. `lab/ingest.py` exits 1 if any bot's `show_config` says it is not dry run. `lab/live.py` is the entry point a live bot would use: it refuses while its own guard is on, and nothing deploys it (`docs/live.md`).

## Run locally

Build the image once: `docker build -t trader .` The commands below use it; mount `-v "$PWD:/repo"` to run uncommitted code.

One bot (roster row 0), API on localhost:8080:

```sh
printf 'kraken-sample kraken SampleStrategy\n' > /tmp/roster.tsv
docker run --rm -p 8080:8080 -v "$PWD:/repo" -v /tmp/roster.tsv:/etc/freqtrade-league/roster.tsv \
  -e LEAGUE_INDEX=0 -e FT_API_PASSWORD=change-me \
  -e FT_API_JWT_SECRET="$(openssl rand -hex 32)" -e FT_API_WS_TOKEN="$(openssl rand -hex 16)" \
  --entrypoint python trader /repo/lab/league.py
```

Migrations and tests against a throwaway Postgres:

```sh
docker network create trader-test
docker run -d --rm --name trader-pg --network trader-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=trader postgres:17
export DATABASE_URL=postgresql://postgres:test@trader-pg/trader
docker run --rm --network trader-test -e DATABASE_URL -v "$PWD:/repo" --entrypoint bash trader \
  -c 'python lab/migrate.py && python lab/migrate.py && python -m pytest -q -p no:cacheprovider lab/tests'
docker rm -f trader-pg && docker network rm trader-test
```

A quick analysis of two strategies: run a backfill into the mounted `/lab` first (below), then `python lab/migrate.py && python lab/analyze.py` in the test container with `-e LAB_RESULTS=/lab -e ANALYZE_EXCHANGES=binanceus -e REGIME_EXCHANGE=binanceus -e "ANALYZE_STRATEGIES=Strategy001 SampleStrategy"`. It reads which candles exist from `data_coverage`, so the backfill has to write there too. A venue x timeframe is analyzed only once every whitelisted pair has been backfilled to its oldest candle, so a venue mid-backfill (okx's first pass) is skipped with `not fully backfilled` instead of caching years run on a few pairs. Two strategies over about a year of binanceus data took under a minute.

Analysis knobs (env): `ANALYZE_EXCHANGES` (`binanceus coinbase`), `ANALYZE_STRATEGIES` (empty means all), `ANALYZE_WORKERS` (at most 6 backtests at once), `ANALYZE_MEMORY_GB` (10: the estimated memory of the tasks in flight together; keep it 2 GB under the pod limit), `BIAS_EXCHANGE` (`binanceus`), `LOOKAHEAD_DAYS` (120), `RECURSIVE_DAYS` (30), `REGIME_EXCHANGE` (`coinbase`, the longest BTC history), `CHUNK_TIMEOUT` (3600 s).

Backfill knobs (env): `BACKFILL_EXCHANGES` (`binanceus coinbase kraken`), `TIMEFRAMES` (`1m 5m 15m 30m 1h 4h 1d`, each capped at what the exchange offers), `FORWARD_EXCHANGES` (`kraken`), `PREPEND_FROM` (`20150101`), `PREPEND_FLOORS` (`1m=20240101`: space-separated `<timeframe>=<date>` pairs, the oldest candle a timeframe is prepended to), `LAB_RESULTS` (`/lab`). For a quick local run, add `-e TIMEFRAMES='4h 1d' -v "$HOME/.cache/trader-lab:/lab"` to the test container and run `python lab/backfill.py`.

## The analysis

`lab/analyze.py` runs nightly and is incremental:

- **Chunks.** Each strategy x exchange is backtested per calendar year, from the first year with candles to today, each year from the same 1000 USD wallet. A chunk is keyed by the strategy file's hash, both config files, the freqtrade version and its dates, so a past year runs once and is cached; a normal night redoes only the current year. Changing a strategy or a config reruns all of its years. A failed chunk is retried on the next two nights, then left failed until its key changes. A run is marked failed when more than 10% of its chunks fail.
- **Trades.** Every backtest trade is stored (`backtest_trades`), so every metric is recomputed from trades, never copied from freqtrade's summary.
- **Bias.** `lookahead-analysis` on `LOOKAHEAD_DAYS` of binanceus and `recursive-analysis` on `RECURSIVE_DAYS`, rerun when the file changes. Lookahead bias means the backtest saw the future: the strategy ranks last and is never suggested. Recursive bias means an indicator still moves at the strategy's own startup candle count; it is shown as a warning, because the indicator may not feed a signal. A check freqtrade refuses (for example a startup candle count of 0) is stored as "not checked" with freqtrade's reason and retried weekly.
- **Windows.** The chunks are chained into one daily return series per strategy x exchange, then measured over the last 30, 90, 180 and 365 days and the whole history: return, CAGR, Sharpe with a 90% block-bootstrap interval, deflated Sharpe, Sortino, Calmar, max drawdown, profit factor, expectancy with its interval, win rate, exposure and the share of months up.
- **Fees.** Each window also has the per-trade edge before fees (close over open, since research trades spot and long only) with its own 90% interval, and the mean round-trip fee a trade paid. A venue whose round-trip fee is below the low end of that interval keeps the edge. The Strategies page has a fees-by-venue table counting the strategies with an edge before and after fees. Backtests use each exchange's retail taker fee: binanceus is about 0.2% a round trip, coinbase about 2.4% (1.2% a side), which is why every coinbase backtest ends close to ruin. More coinbase pairs would add compute and no information; research scale goes to the cheap venues.
- **Real fees.** Each `user_data/exchanges/*.json` sets `fee`: the venue's entry-tier taker fee plus 0.05% slippage per side (binanceus 0.02%, kraken 0.80%, coinbase 0.90%; checked 2026-11, the coinbase tier from a search snippet only). Freqtrade uses it in backtests and dry runs, and without it falls back to ccxt's stale defaults, which understated kraken about fivefold. The 0.05% is a guess until live fills can measure it. Update it when a schedule changes: that reruns every chunk. Dry-run trades closed before 2026-11-05 carry the old fee, so drift on them is overstated.
- **Worst open.** Returns and drawdown are realized on close, so a strategy that sits on its losers looks perfect until a stop hits: UniversalMACD's exit signal can never fire, and it showed 0% drawdown over 180 days against 75% over its whole history. Worst open is the deepest any trade went against its entry, from the candle lows it lived through.
- **Over hold.** Each window compares the strategy with holding BTC with the same share of capital: daily return less capital share times BTC's daily return (Coinbase closes), annualised, with a 90% block-bootstrap interval. Capital share is each trade's stake over its chunk's starting wallet, weighted by time held, so one open trade of three counts as a third. A long-only strategy that only rode the market scores about zero. Most strategies trade alts, so this measures against the market leader, not against their own pairs.
- **Deflated Sharpe** is the probability that a Sharpe beats the best one luck alone would give across every strategy tried in that window: the expected best of that many strategies with a true Sharpe of zero, whose estimates vary only by sampling noise. It does not use the spread of the strategies' own Sharpes, which is mostly fee drag and would set the bar near an annualized 10. With 89 strategy x exchange series over a year, the bar is about an annualized 2.5, so a raw Sharpe of 1 is not evidence; a DSR near 1 is. The number of strategies is the effective count: the participation ratio of the eigenvalues of the correlation matrix of their daily returns, (sum l)^2 / sum l^2. Independent series count once each and copies count once together, so the same strategy on five venues, or five near-copies, no longer raise the bar five times. `trials_raw` keeps the plain count.
- **Overfit.** The probability of backtest overfitting, per exchange and window of 90 days or more (`overfit`), by combinatorially symmetric cross-validation (Bailey, Borwein, Lopez de Prado and Zhu): the days of every strategy tested there split into 16 blocks, and each of the 12,870 ways to take half of them in sample asks whether the in-sample best strategy lands below the median on the other half. Near 0 means the best backtest there tends to stay good; above 0.5 means picking it is worse than picking at random. A strategy needs 90% of the window's days to be ranked; its missing days count as flat.
- **Trades still needed.** Per window, (1.645 x per-trade spread / mean per trade)^2 less the trades so far: how many more trades the low end of the 90% interval needs to clear zero if the mean and spread hold. Empty when the mean is not positive, since no sample size gets there. At a per-trade spread of 4.9%, an edge of +0.5% needs about 260 trades, +0.2% about 1,600 and +0.1% about 6,500.
- **Estimates.** A seeded block bootstrap (10-day blocks, 5000 paths) of the last 365 days gives 30 and 90-day return bands (p10, p50, p90), the chance of a loss and the expected worst drawdown. Same data, same bands. It needs at least 180 days.
- **Regimes.** Every day gets a BTC trend (above or below its 200-day mean) and a volatility third (30-day realized volatility against its own history), and the strategy page splits returns by both.
- **Drift.** Each league bot's strategy is backtested on its own exchange from the bot's start to now, with the bot's config, and set against its dry-run trades (`drift`). An entry matches when the backtest entered the same pair within one candle; the gap is live minus backtest profit per trade over trades closed on both sides. A low match rate says the backtest does not model what the bot does (startup candles, data gaps, a different pairlist); a steady negative gap is fill or fee error the backtests leave out. It needs weeks of trades to mean anything. The Overview shows the match per bot and each bot page has the comparison.
- **Correlations.** Each strategy's daily returns against every strategy the league runs on that exchange, over the last 365 days; near 1 means it would only duplicate a bot already running.

The leaderboard ranks by the low end of the Sharpe interval, so a strategy has to look good on its unlucky draw. Rows under 20 trades sink, and lookahead-biased rows go last. Promoting a strategy is still a human edit to the roster in home-config.

## Walk-forward

A backtest of a strategy as it is says nothing about tuning it, and hyperopt's best result on a span is fitted to that span. `lab/walkforward.py` runs weekly to measure how much of hyperopt's gain survives on data it never saw:

- **Candidates.** The league's strategies, then the promote suggestions, then the watch ones by score, then the best 365-day expectancy low end on each venue in `WF_EXCHANGES`, taking one venue's next best after another, all loadable and lookahead-clean, up to `WF_LIMIT`. coinbase is not a walk-forward venue: its 2.4% round trip leaves nothing to tune.
- **Venues.** Every candidate runs on every venue in `WF_EXCHANGES`, one fold row per venue, once that venue's candles for its timeframe are fully backfilled (the same coverage guard as the analysis). A fold whose training span starts before the venue's first candle is skipped, and the run's `skipped` list says why.
- **Memory.** Folds share `WF_MEMORY_GB` by their estimated size (training days plus a month of candles across the venue's pairs, at twice a backtest's load), so a 22-pair venue does not start ten hyperopts at once.
- **Folds.** For each of the last `WF_FOLDS` whole calendar months, hyperopt tunes the strategy on the `WF_TRAIN_DAYS` before it (`--spaces default`: ROI, stoploss, trailing, max open trades and any declared buy and sell parameters) with a random state seeded by strategy and month, so a rerun gives the same answer. Then the month is backtested as is and with the tuned parameters, and the training span is backtested with them too (in sample). Every per-trade profit ratio is kept.
- **Isolation.** Hyperopt works on a copy of the strategy's folder in a temporary userdir, because it writes its params file next to the strategy, where every nightly backtest would load it.
- **Cache.** A fold is keyed by the strategy file, both configs, the freqtrade version, its dates and the hyperopt settings, so a normal week runs only the newest month, and a changed file reruns all of that strategy's months. Failures retry like analysis chunks; a month with no candles is stored once. A fold where no epoch reached `WF_MIN_TRADES` training trades is kept, untuned, with that as its note.
- **Reading it.** The Walk-forward page pools each strategy's months: how many the tuned parameters beat, the per-trade profit as is and tuned with 90% bootstrap intervals, and walk-forward efficiency, the tuned return per day out of sample over the same in sample. Near 1 the tuning carries over; near 0 or below, hyperopt fitted noise. The strategy page has the months one by one.

| Env | Default | What it is |
|---|---|---|
| `WF_EXCHANGES` | `binanceus` | Space-separated venues folds run on |
| `WF_FOLDS` | `6` | Test months, the newest whole ones |
| `WF_TRAIN_DAYS` | `180` | Training span before each test month |
| `WF_EPOCHS` | `150` | Hyperopt epochs per fold |
| `WF_LOSS` | `SharpeHyperOptLossDaily` | Hyperopt loss |
| `WF_MIN_TRADES` | `10` | Training trades an epoch needs to count |
| `WF_LIMIT` | `16` | Candidates per run |
| `WF_WORKERS` | `6` | Folds at once; each hyperopt runs on one core (`-j 1`) |
| `WF_TIMEOUT` | `10800` | Seconds before one hyperopt is killed |
| `WF_MEMORY_GB` | `10` | Estimated memory the folds running at once may hold |
| `WF_STRATEGIES` | all | Space-separated names to limit a run to |

## Suggestions

`lab/suggest.py` runs at the end of every analysis (alone: `python lab/suggest.py`) and rebuilds `suggestions` from the derived tables. Every threshold is in `lab/suggest.toml`; each suggestion stores every gate it was held to with the value seen, so the site shows why.

- **Promote**: a strategy and exchange the league does not run yet that is lookahead-clean, has at least 30 trades in the last 365 days, has the low ends of its expectancy and Sharpe intervals above zero, a deflated Sharpe of at least 0.90, at least 70% of at least two full calendar years profitable, a drawdown under 35%, daily returns that do not track a league strategy (|rho| at most 0.70), the trades and expectancy gates met on at least two venues (`min_venues`), so an edge on one thin book stays a watch, and an overfit probability on its venue of at most 0.5 (`max_pbo`). The action is the exact `roster.tsv` line and the new replica count.
- **Watch**: lookahead-clean candidates one gate short, best Sharpe interval first, so a near miss is visible before it qualifies.
- **Demote**: a league bot whose own dry run lost money over 60 days with at least 20 trades, whose strategy's 365-day backtests on that exchange lose even at the high end of the expectancy interval (kraken has no history, so for its bots every research exchange must agree), or whose strategy has lookahead bias. Rows map to pod ordinals and each pod keeps its own sqlite, so the action says to replace the row in place and clear that pod's PVC, never to delete the row.
- **Retire**: lookahead bias or a load error. It supersedes a fix for the same file.
- **Fix**: the recursive check found an indicator still moving at the startup candle count, or could not run because the strategy declares none.
- **Retune**: a lookahead-clean strategy whose tuned parameters beat it as it is on each of the newest two walk-forward months (at least 5 tuned trades each), and, pooled over every month, has at least 30 tuned trades, the low end of the 90% interval of its per-trade gain over as is above zero (so a tie by luck does not pass), and the low end of its own 90% interval above zero. The action is the newest fold's params file, to save next to the strategy.

A suggestion keeps its `first_seen` while it holds, so "since Nov 05" on the site is how long the evidence has pointed the same way; one that stops applying is deleted.

## Stress gates

Four gates in `lab/confidence.py` reuse trades already stored, so they add no trial to the register: the low end of expectancy must clear a slippage guess, the same trades in random order must stay inside the hard stop at Stage 1 stake, the strategy must stay positive without its best pair, and it must be positive on at least two venues.

## Book

`lab/book.py` runs after the score (alone: `python lab/book.py`) and rebuilds `book`, shown at `/book`. Each scored strategy with enough walk-forward months gets a 5th, 50th and 95th percentile return over 1 and 5 years, from a block bootstrap of its out-of-sample monthly wallet returns, and the same for BTC over those months. With no score there is no number: the page says there is not enough evidence. The ranges assume the tested months repeat, so a short basis makes a 5-year range look firmer than it is. The page also lists each stage's capital, what it can lose at each limit and its running costs, from `lab/suggest.toml` [book].

Analysis also fills `benchmarks` from the regime venue's daily closes: holding BTC, holding BTC and ETH, monthly DCA into BTC, and BTC only above its 200-day mean, each over the last 1, 3 and 5 years and the whole shared history. Tax rates there are guesses, in config.

## Confidence

`lab/confidence.py` runs after suggestions (alone: `python lab/confidence.py`) and rebuilds `confidence`, one row per strategy and exchange, shown on `/confidence`. Promote decides who enters the dry-run league; this decides who could touch money, so its gates are stricter. It is advice: nothing here touches a bot, and `lab/league.py` still refuses real money.

- **Score**: 100 x P(true net expectancy per trade > 0). Each strategy's pooled tuned walk-forward trades, shrunk toward every strategy walk-forward has tried. The population mean is capped at zero, so a rising market does not lift everyone. No score below 20 strategies with 30 trades each. Until then `/confidence` and `/book` show how many count so far and each strategy's raw out-of-sample average per trade, marked unscored.
- **Ready**: every gate passes and the score is at least 90. A failed gate shows as "not ready: <gate>" whatever the score. Ready means worth a small capped experiment, not safe.
- **Gates**, thresholds in `lab/suggest.toml` [confidence]: bias, costs at the venue's configured fee, 250 out-of-sample trades, expectancy interval low above zero, deflated Sharpe, overfit probability, months positive, BTC regime spread, excess over hold, worst open loss, cost stress, drift, dry run length, score.
- **Out of sample** means the tuned returns, months hyperopt never saw. So the score speaks for tuning monthly and trading the next month, not for the file as it stands, and candidates were chosen on a trailing year that overlaps those months.
- **Lockbox**: months from `[lockbox] start` on stay out of walk-forward, retune, the score and the book. The date is fixed, so the box grows until a strategy clears everything else. Then it gets one look: a PR moves the date, and a migration in that PR adds the look to `trials`. Two limits: `strategy_windows` still covers those months, so the gates reading it (overfit, excess over hold) have seen them, and the candidates were chosen on a trailing year that overlaps the box. Folds stored before the lockbox stay on `/walkforward`.
- **Stop rule**, written before the Saturday 2026-11-07 run: if no strategy reaches ready after that run and the remaining stress tests, the recommendation is core hold only and the live work ends. Results do not move a threshold afterwards.
- **Trial register**: `trials` counts every strategy, exchange and tuned parameter set ever tried, and keeps a row when its strategy goes, so the bar never falls. The deflated Sharpe uses that count at the window's effective share.

## Adding a strategy

Drop the `.py` in `user_data/strategies/` (any subfolder). It must import on the pinned image: Python 3.14, NumPy 2 (`np.nan`, not `np.NaN`), no `ta` package. Check with `freqtrade list-strategies --recursive-strategy-search --userdir user_data -c user_data/config.base.json -c user_data/exchanges/binanceus.json`. The next nightly analysis backtests its whole history and checks it for bias. Give its class a line in `lab/families.toml`, or `/coverage` lists it as untagged and the tests fail.

## Staged families

`lab/families/` holds whole method families waiting to be switched on: `candles/`, one strategy per TA-Lib candlestick pattern that can signal bullish, and `chart/`, one per long signal in `lab/patterns.py`. Each family shares one fixed exit (ROI table, stoploss, time stop), and its `family.toml` holds that exit and every parameter. The strategy files are generated from it: edit the toml or `lab/patterns.py`, then run `python lab/families/generate.py`; `lab/tests/test_families.py` fails while a file is stale. The nightly analysis searches only `user_data/strategies/`, so these add no trials yet; the pre-registered stop rule reads the trial count on 2026-11-07. CI still checks that each one loads and that one of each passes the lookahead check on synthetic prices. Switching a family on is a PR that moves its folder to `user_data/strategies/<family>/` and drops the test that says it is not scanned.

`ml/` is the machine-learning family, hand-written rather than generated: `MLLightGBMBaseline`, a FreqAI LightGBM regressor that predicts the mean close over the next 24 hourly candles and goes long when the prediction clears 1%, with its FreqAI settings in `ml/freqai.json` (30 training days, 7-day windows, `n_jobs` 2: LightGBM's default of one thread per core thrashed for 90 seconds per fit on a loaded hp-victus, against under 5 seconds pinned). It needs lightgbm, which only the ML image has: `ghcr.io/kblack0610/trader-ml`, this same Dockerfile built with `--build-arg FREQTRADE_TAG=2026.9_freqai` (2.7 GB against 1.3 GB), so the bots never run on it. CI builds it and runs `lab/tests/test_ml.py` there, which checks that each 7-day window is predicted only by a model fit before it; the plain image skips that test. It stays off until walk-forward purges trades across fold boundaries, since a 24-candle label spills into the next fold.

## Price data

`lab/backfill.py` owns the candles; nothing else downloads them. Per exchange and timeframe:

- A series that has not reached the exchange's oldest candle gets a `download-data --prepend --timerange 20150101-` pass. One call reaches the listing date (binanceus from 2019, coinbase from 2015 for BTC). 1m stops at its `PREPEND_FLOORS` date, 2024-01-01: the 1m files are about 30 times the 30m ones, and only a few scalping strategies (BinHV45, CCIStrategy, Low_BB, ReinforcedSmoothScalp, Scalp, SmoothScalp) trade on 1m.
- Then every series gets an append pass up to now. A prepend drops a series' newest candle, so the append always runs after it.
- When a prepend leaves the first candle where it was, the series is marked `oldest_reached` and only appended from then on.
- Kraken serves no OHLCV history, only its newest 720 candles per timeframe. It is collected forward through ccxt and merged into the same Freqtrade files. A run at least every 60 hours keeps 5m gap-free; a longer outage leaves a gap that `data_coverage` counts. These candles exist nowhere else until the next quarterly archive.
- Kraken's past comes from its published OHLCVT archive instead (`lab/archive.py`): the first backfill reads each series from the five-part zip by HTTP range, so the 10 GB file is never downloaded or unpacked, and marks it `oldest_reached`. A failed read is retried next hour and does not stop collection. The archive leaves out candles with no trades, so old thin periods count as gaps in `data_coverage`; Freqtrade fills those on load. It has no 30m, which is built from 15m, and 1m keeps the `PREPEND_FLOORS` date.
- The full archive ends at 2026-09-30, so a hole runs from there to the first collected candle of each timeframe. Kraken publishes each quarter after it ends: once `Kraken_OHLCVT_2026Q4.zip` is out, run `python lab/archive.py <url>` once in a backfill-mounted pod, between backfills, to close it.

### Venues

| Venue | Pairs | Fee per side in backtests | Used by |
|---|---|---|---|
| binanceus | 10 USD | ccxt's default | league bots, research |
| coinbase | 10 USD | ccxt's default | league bots, research |
| kraken | 10 USD | ccxt's default | league bots (forward candles only) |
| okx | 22 USDT | 0.20%: 0.15% taker plus 0.05% slippage | research only, no bot |
| kucoin | 18 USDT | 0.15%: 0.10% taker plus 0.05% slippage | research only, no bot |

binanceus has no USD pair trading $5M a day, so its backtests assume fills its books may not give; okx and kucoin add pairs that trade millions a day, where a backtest's fills are believable. Their whitelists come from `python lab/pairs.py <exchange>`: pairs over $2M a day for the last 30 days, with a daily candle by 2022-01-01, at most 40. Their `fee` key is the stressed cost, set by hand in the exchange file; Freqtrade applies a `fee` key to dry-run fills too, which is why the bot venues do not get one here.

**Survivorship.** Those whitelists are today's liquid survivors. A coin that was liquid in 2022 and died since is not in them, so the research venues' early years flatter every strategy a little. Freqtrade cannot backtest a point-in-time volume list; read okx and kucoin results as an upper bound and look for agreement with binanceus.

The research venues are backfilled by their own job (`5m` and up, no 1m), so their slow first pass never holds back the hourly candles of the bot venues. OKX serves 100 candles per request, so its first pass takes several runs; each run keeps the pairs it finished.

**Perpetual swaps.** `lab/futures.py` collects funding rates and daily open interest for the okx and kucoin swap of each whitelisted pair through ccxt (Freqtrade cannot download kucoin futures) into `funding_rates` and `open_interest`, and is their only writer. Free history is short: okx serves funding from about three months back and open interest from 180 days; kucoin serves funding from 2024 (`FUNDING_FROM`, the first run's floor) and open interest from about 98 days. So collection runs daily and the rows are kept forever. Each run resumes funding from the newest stored row; open interest is refetched over its window, so a missed day is refilled. Knobs: `FUTURES_EXCHANGES` (`okx kucoin`), `FUNDING_FROM` (`2024-01-01`). The first run took about two minutes, later ones about 30 seconds.

The first run fetches full history (5m since 2019 on binanceus is the long pole) and can take hours; later runs take under a minute. Exchanges run in parallel, each with its own rate limit.

## The database

- `001`: `bots`, `bot_trades` (one row per trade, keyed by bot, trade id and open date, so a bot whose sqlite is lost keeps its old trades), `bot_snapshots` (hourly from ingest; rows with `source = 'report'` came from the retired daily report), `runs`.
- `002`: `data_coverage`, first and last candle, rows, gaps and missing candles per exchange x pair x timeframe, written by every backfill run.
- `003`: the analysis. `strategies` (every file found, its hash and load error), `backtest_chunks` and `backtest_trades` (the cache and its trades), `bias_checks`; derived, rebuilt in one transaction at the end of each run: `strategy_daily`, `strategy_windows`, `estimates`, `regimes`, `correlations`. `runs` rows of kind `analysis` carry the run summary in `raw`.
- `004`: clears bias checks that errored before subfolder strategies could load, so they rerun.
- `005`: `suggestions`, one row per kind, strategy, exchange and bot, with its gates as JSON and when it was first and last seen.
- `012`: `trials`, the register of everything tried, and `confidence`, the score and gates per strategy and exchange.
- `013`: `watch`, one row per bot rewritten by `lab/watcher.py`: equity, the day's opening equity, breach, drill
- `014`: `book`, projected return ranges per scored strategy and for holding BTC.
- `015`: `benchmarks`, the plain alternatives to an active strategy.
- `018`: `funding_rates` and `open_interest`, measured swap data per exchange, swap symbol and time, written by `lab/futures.py`.
- `019`: `venues`, `markets`, `outcomes` (each outcome's latest quote), `resolutions` and `market_sweeps` (one row per recorder run), written by `lab/markets/recorder.py`. Quote history is parquet in MinIO, never Postgres.
- `020`: `market_links` (the same event across venues, proposed, confirmed or rejected) and `market_link_asks` (each market the LLM was asked about), written by `lab/markets/links.py`.

 A schema change is a new numbered file; never edit an applied one.

## The site

`web/app.py` reads the `trader` database through a role that only has SELECT, and has no trade controls. Every page but `/healthz` needs a Forgejo sign-in: `/login` sends the browser to Forgejo's OAuth2 authorize page, `/oauth/callback` swaps the code for the Forgejo login, and only logins in `ALLOWED_USERS` get a 30-day cookie signed with `SESSION_KEY`. The site keeps no session store, so a restart signs nobody out and rotating `SESSION_KEY` signs everyone out.

| Env | What it is |
|---|---|
| `DATABASE_URL` | The read-only role's connection string |
| `TRADER_WEB_BASE_URL` | The site's own URL; the OAuth redirect is `<base>/oauth/callback`, and cookies are `Secure` when it is https |
| `FORGEJO_URL` | Forgejo's public URL (its `ROOT_URL`), where the browser signs in |
| `FORGEJO_API_URL` | Where the server swaps the code and reads the user; defaults to `FORGEJO_URL`, in-cluster it is the Forgejo service |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` | The Forgejo OAuth2 application |
| `SESSION_KEY` | Cookie signing key, `openssl rand -hex 32` |
| `ALLOWED_USERS` | Comma-separated Forgejo logins, default `kblack0610` |
| `TRADER_WEB_TZ` | Time zone pages show times in, default UTC |
| `METRICS_PORT` | When set, Prometheus metrics are served on this port (home-config uses 9100), apart from the site port so the Ingress never exposes them |

The site refuses to start when a sign-in variable is missing. Locally, against a copy of the data:

```sh
docker run --rm -p 127.0.0.1:18765:8000 --entrypoint uvicorn -e DATABASE_URL=... -e FORGEJO_URL=https://git.example \
  -e OAUTH_CLIENT_ID=x -e OAUTH_CLIENT_SECRET=x -e SESSION_KEY=$(openssl rand -hex 32) trader web.app:app --host 0.0.0.0 --port 8000
```

Sign-in cannot complete against a fake Forgejo; mint a cookie with `auth.make_session(auth.Config(), "kblack0610")` under the same `SESSION_KEY` and send it as `trader_session`.

`/coverage` joins `lab/families.toml` to the results already stored: whole-history backtests (`strategy_windows`), walk-forward test months, the trial register and the confidence gates, as a family by venue matrix with one row per strategy below it. A strategy class under `user_data/strategies` with no line in the toml shows in an untagged row, and a name only the results still hold (a deleted strategy, the three dropped hyperopts) in a gone row.

### Watcher

`lab/watcher.py` is a Deployment, not a job. Every 60 seconds it reads `/show_config`, `/profit` and `/balance` from each bot and writes one `watch` row per bot. Equity is `starting_capital` plus `profit_all_coin`, which values open trades at the current rate. The bot's `/balance` total is not used, since its wallet refreshes at most hourly.

A bot is breached when equity falls `[watcher] day_loss` of capital below the first reading of the UTC day, and stays breached until the next one. This is Stage 0, so a breach only raises an alert. The day opens at the first reading, so a bot unseen at 00:00 UTC opens late, and a watcher started mid-day counts from then.

The drill proves the stopping path on one dry-run bot. Run these in the watcher pod:

```
python lab/watcher.py --drill <bot>
python lab/watcher.py --drill-end <bot>
```

The first flags the bot; the next pass marks it breached and POSTs `/stopentry`, which keeps managing open trades. `stopped_at` minus `drill_at` in `watch` is the time taken, and the plan's gate is two cadences. The pass repeats the stop while the drill lasts, because a restarted bot comes back running. The second clears the breach and POSTs `/start`.

### Metrics

`web/exporter.py` reads the database on each scrape, so the numbers are as fresh as the tables: bot state is as old as the newest hourly ingest. home-config alerts on these:

| Metric | Labels | Meaning |
|---|---|---|
| `trader_db_up` | | 0 when the scrape could not read the database; every other metric is absent then |
| `trader_bot_dry_run` | `bot` | 1 dry run, 0 trading real money, per the newest snapshot |
| `trader_bot_snapshot_age_seconds`, `trader_bot_snapshot_error` | `bot` | Age of the newest snapshot, and 1 when ingest got an API error instead of numbers |
| `trader_bot_open_trades`, `trader_bot_closed_trades`, `trader_bot_profit_pct` | `bot` | The newest snapshot's numbers |
| `trader_ingest_age_seconds` | | Since any snapshot was written |
| `trader_watch_age_seconds` | | Since the watcher last finished a pass |
| `trader_watch_unseen_seconds`, `trader_watch_running`, `trader_watch_breached`, `trader_watch_day_loss_ratio` | `bot` | Since the last good read, 1 when taking entries, 1 after a daily loss breach, and the loss so far today as a share of capital |
| `trader_run_started_timestamp_seconds`, `trader_run_finished_timestamp_seconds`, `trader_run_ok`, `trader_run_last_ok_timestamp_seconds` | `kind` (`analysis`, `walkforward`) | Newest run started, newest run finished and whether it succeeded, newest success |
| `trader_candle_age_seconds`, `trader_coverage_age_seconds`, `trader_coverage_series` | `exchange` | Newest stored candle, newest backfill write, series stored |
| `trader_futures_age_seconds` | `kind`, `exchange` | Newest funding (`funding`) or open interest (`open_interest`) row |
| `trader_suggestions` | `kind` | Open suggestions |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A `watch` row has `error` set and `seen_at` falling behind | That bot's API did not answer, or an open trade has no price so `profit_all_coin` is NaN | The row clears on the next good read; TraderWatchBotBlind fires after 5 minutes |
| `migrate: 00N_x.sql changed after it was applied` | An applied migration file was edited | Revert the edit and put the change in the next numbered file |
| `DATABASE_URL is not set` (exit 2) | The job has no `trader-db` secret mounted | home-config `apps/freqtrade/trader-db-secret.sops.yaml` holds it |
| An ingest snapshot has `error` set and no trades moved | That bot's API was unreachable that hour | The next run catches up: trades are upserted from the bot's own history |
| `File /repo/user_data/logs exists already and is not a directory` | freqtrade's userdir check-then-mkdir races when backfill starts one freqtrade per exchange at once | The image creates every userdir subfolder (`Dockerfile`); if it recurs, a new subfolder is missing from that `mkdir` |
| `Historic klines not available for Kraken` | Kraken serves no OHLCV history through ccxt | Backfill collects Kraken forward (`FORWARD_EXCHANGES`); the analysis leaves it out of `ANALYZE_EXCHANGES` until months of candles exist |
| Kraken `--dl-trades` crawls (minutes per day of history) | BTC/USD prints about 300k trades a day, and freqtrade reloads and re-converts the whole trades file every run | Not used: forward OHLCV collection gets the same candles in one call per series |
| A Kraken series in `data_coverage` has `gaps` above 0 | The backfill job did not run for longer than 720 candles of that timeframe (60h at 5m) | The gap is permanent; check why the CronJob stopped |
| `backfill: <ex>: no 4h candles on this exchange, skipped` | coinbase offers no 4h | Expected; 4h strategies cannot backtest on coinbase |
| A series stays `oldest_reached = false` | The prepend pass failed or still moved the first candle | It is retried next hour; the job log line `backfill: <ex>: ... ERROR` says why |
| `Orderbook not available for Coinbase Advanced` | freqtrade has no order book for coinbase | `exchanges/coinbase.json` sets `use_order_book: false` for entry and exit pricing |
| A chunk failed with `exit 0: ...` | freqtrade exited 0 on an error and wrote no result zip | The zip is the proof of a run; the chunk's `error` holds freqtrade's last error lines, shown on the run page |
| A run's Skipped list says `<strategy> on <ex>: no 1m candles` | The strategy's timeframe is not backfilled there (BinHV45 wants 1m) | 1m is backfilled from 2024-01-01; until the first pass finishes, or on an exchange that serves no 1m, this is expected |
| Bias checks or chunks fail with `Impossible to load Strategy '<name>'` while `list-strategies` finds it | The command ran without `--recursive-strategy-search`, so only top-level files resolve | Every freqtrade call in `lab/` passes it; `test_a_strategy_in_a_subfolder_backtests_and_checks` covers backtest and bias |
| One strategy still fails with `Impossible to load Strategy` even with recursive search | freqtrade finds a strategy by grepping its file for the literal `class <name>(`; `class <name> (IStrategy)` imports fine but never matches | Remove the space. Discovery stores this as the strategy's load error, so it shows on the site instead of failing every chunk (`test_a_class_freqtrade_cannot_find_by_name_is_a_load_error`) |
| A strategy shows `fails to load` | It does not import on the pinned image | Fix it or delete it; CI fails on a load error, so this only shows for a file that broke after a freqtrade bump |
| The analysis stops logging progress for hours while its pods stay busy, and Postgres logs `invalid input syntax for type json` `Token "NaN" is invalid` | A recursive check's diff held NaN, which Python's json writes and jsonb rejects; leaving the worker pool then waited for every queued chunk before the error surfaced | Non-finite numbers are stored as null (`test_a_non_finite_number_in_a_check_is_stored_as_null`), and an error in the result loop cancels the queue so the run fails within one chunk (`test_a_store_error_ends_the_run_without_running_the_queue`) |
| Recursive check `not checked: invalid startup candle count of 0` | The strategy sets no `startup_candle_count` | Set one in the strategy; the lookahead verdict still counts |
| `pandas` `get_loc() got an unexpected keyword argument 'method'` or `fillna(method=...)` | pandas 3 removed both | Use `get_indexer(..., method=...)` and `ffill()`; FixedRiskRewardLoss was deleted for the first |
| kucoin backfill fails with `429000 Too many requests. User-level rate limit exceeded` and no series reaches its oldest candle | ccxt's default kucoin pace (7.5 ms x cost 3 per candle request) outruns kucoin's public limit, so prepends and even market loads fail every run | `user_data/exchanges/kucoin.json` sets `rateLimit` 25 in `ccxt_config` and `ccxt_async_config` (75 ms per candle request, about 13 a second) |
| coinbase backfill logs hundreds of `429 Too Many Requests` on its first run | The first deep 5m prepend pulls ten years of candles and outruns coinbase's public rate limit | freqtrade retries; the run that fails is finished by the next hour's run, and later runs append about 300 candles each |
| The site answers `Sign-in expired or was not started here` (400) | The callback arrived without the state cookie `/login` set, or more than 10 minutes later | Open `/login` again; a link to `/oauth/callback` from history cannot sign in |
| The site exits with `trader-web: missing ...` | A sign-in variable is not set | home-config `apps/freqtrade/trader-web-oauth-secret.sops.yaml` holds them |
| A walk-forward month says `no hyperopt epoch reached N training trades` | The strategy trades too rarely on the training span for any epoch to count | Expected for slow strategies; the month keeps its as-is backtest. Lower `WF_MIN_TRADES` or raise `WF_TRAIN_DAYS` to tune it |
| `The following spaces were not found in the strategy: buy, sell` in a walk-forward log | The strategy declares no hyperopt parameters | Harmless: `--ignore-missing-spaces` tunes ROI, stoploss, trailing and max open trades anyway |
| `token is a required property` | The config schema checks the telegram block even when disabled | There is no telegram block; do not add one back empty |
| `'' is too short` or a JWT length error | `api_server` secrets are validated at load | `league.py` writes them from env at start; use `openssl rand -hex 32` for the JWT secret |
| `ModuleNotFoundError: ta` | The image has no `ta` package | GodStra, Heracles and Zeus were deleted for this |
| Futures strategies missing | US exchanges are spot only | `user_data/strategies/futures/` was deleted |
| binance.com or bybit refuse to connect (451, 403) | They geo-block US IPs, market data included | okx and kucoin serve public market data to US IPs; they are research venues, with no account |
| The analysis pod is `OOMKilled` within a minute | Too many 1m tasks at once: a year of 1m on 10 pairs peaks at 4 GB, a 5m year at 1.1 GB | The pool admits tasks by estimated memory (`ANALYZE_MEMORY_GB`); if a new venue or timeframe still overflows, re-measure and update `TASK_BASE_GB` and `GB_PER_MCANDLE` in `analyze.py` |
| Every walk-forward fold fails with `Fatal exception! \| 1` | Hyperopt's `--random-state` must fit in 32 bits for numpy and optuna | Fixed in `metrics.seed_of`; the fold error now keeps the whole message |
| `pod ordinal N has no roster row` | StatefulSet replicas exceed roster rows | Set replicas to the roster row count in home-config |
