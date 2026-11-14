# freqtrade_trading_attempt

Strategy code and tooling for a Freqtrade dry-run league: a set of paper-trading bots, each running one strategy on one exchange, plus a nightly analysis that backtests every strategy over all the price history there is and ranks them with confidence intervals, bias checks and return estimates. No real money, ever.

The league runs on home-k3s. Its deploy (roster, StatefulSet, CronJobs, secrets) lives in home-config at `apps/freqtrade/`. Every pod runs one image, `ghcr.io/kblack0610/trader`: the `freqtradeorg/freqtrade:2026.10` release, pinned by digest in the `Dockerfile`, plus psycopg plus this repo at `/repo`. GitHub Actions builds it on each push to master (tags `sha-<7>` and `latest`), and home-config pins it by digest, so a change here goes live only when home-config bumps the digest.

Results go to the `trader` database in the cluster's shared Postgres, which the nightly `pg_dumpall` backs up. The site in `web/` shows them, behind a Forgejo sign-in; home-config deploys it.

## Documentation

[Overview](docs/overview.md) describes features and status. [Architecture](docs/architecture.md) maps code and storage boundaries. [Contributor standards](docs/standards.md) defines the check scope; [deployment](docs/operations/deploy.md) describes the home-k3s delivery path. The [docs index](docs/README.md) links the subject references.

## Layout

`lab` and `web` are packages imported from the repo root: `from lab import metrics`, `from lab.markets import fees`. The image sets `PYTHONPATH=/repo`; a job runs as `python /repo/lab/<job>.py`, or `python -m lab.markets.<module>` for the markets jobs.

| Path | What it is |
|---|---|
| `user_data/strategies/` | Strategies, found with `--recursive-strategy-search`; `catalogue/` holds the families the coverage catalogue found missing (grid, DCA, relative value, squeeze breakout, time-series and cross-sectional momentum, [strategy-coverage.md](docs/strategy-coverage.md)) |
| `lab/families/` | The family generator, and where a family is staged before it is scanned (see Generated families) |
| `lab/patterns.py` | Causal chart-pattern signals: swing pivots, support and resistance breaks, double tops and bottoms, triangles, flags |
| `user_data/config.base.json` | Shared config: dry run, 1000 USD wallet, spot, static pair list |
| `user_data/exchanges/<name>.json` | Per-exchange overlay: name, pair whitelist, pricing quirks; no keys |
| `lab/healthcheck.py` | The image's Docker `HEALTHCHECK`: healthy when the bot API (:8080) or the site (:8000) answers; k8s ignores it and uses home-config's probes |
| `lab/league.py` | Bot entrypoint: picks its roster row from the pod ordinal, layers the row's setup overlay, refuses anything but key-less spot dry run, writes the API config, execs `freqtrade trade` (see Cohorts) |
| `lab/pairs.py`, `lab/universe.py` | By hand: a research venue's whitelist, its most traded pairs with history since 2022 and a volume floor then and now; `user_data/universe.json` records where each list came from and analysis and walk-forward copy it onto their run |
| `lab/futures.py` | Daily job: funding rates and open interest for the okx and kucoin swaps into `funding_rates` and `open_interest` (see Price data) |
| `lab/markets/`, `lab/markets.toml` | Event contracts, kept out of the Freqtrade runtime: the venue adapter interface (`venue.py`), read-only adapters for Kalshi, Polymarket US and global, PredictIt, Manifold and The Odds API (sportsbook odds), the cross-venue linker (`links.py`: rules, LLM proposals, confirmation by resolution), the recorder, a 5-min job writing change-only quotes to MinIO parquet and market metadata to Postgres, its nightly compaction (`compact.py`: one object per venue and day), the history backfill (`history.py`: settled markets, price bars and Kalshi trades from before the recorder started), the backtester (`backtest.py`, `fees.py`: depth-limited taker fills, per-venue fees, settlement at resolution, scored through `metrics.py` and `confidence.py`), and the strategy families (`families.py`: favourite-longshot, base-rate calibration, cross-venue gaps, and a forward-only LLM forecaster), the paper league (`paper.py`: each family variant trades paper money against the recorder's live books, watched by `lab/watcher.py`) and the order drills on the Kalshi demo exchange (`kalshi_trade.py`, demo hosts only) and on Manifold play money (`manifold_trade.py`); see `docs/markets.md`, Recorder, History, Backtest, Families, Paper league and Demo orders |
| `lab/broker.py`, `lab/broker.toml` | Broker accounts into `broker_balances`: the Alpaca paper balance and an order drill, and read-only reads of Robinhood Crypto, Fidelity through SnapTrade, and Kraken (see [platforms.md](docs/platforms.md#brokers-without-a-full-api)) |
| `lab/backfill.py` | Hourly job: price history for every exchange x pair x timeframe into `data/<exchange>/` on the lab PVC, and what it holds into `data_coverage` (see Price data) |
| `lab/analyze.py` | Nightly job: every strategy x exchange backtested in calendar-year chunks over the whole history, lookahead and recursive bias checks, then the derived tables (see The analysis) |
| `lab/variants.py`, `lab/variants.toml`, `lab/grid.py` | Setup overlays the analysis sweeps (max open trades, stake, stoploss, ROI, pair count), and the one grid expander the overlays and the event-contract families share (see Setup overlays under The analysis) |
| `lab/walkforward.py` | Nightly job: hyperopt on the months before each whole month since the venue's first training span, then that unseen month backtested as is and tuned (see Walk-forward), split over `WF_SHARDS` pods |
| `lab/snapshot.py` | `publish`: the lab PVC's candle files to MinIO, content-addressed, with a manifest; `fetch`: the latest manifest's files to a pod's disk, so walk-forward pods run on any node (see Walk-forward, Candles) |
| `lab/calibration.py` | Weekly job: each gate's false pass rate and power, from null and planted-edge strategies run through the whole pipeline on synthetic candles (see Calibration) |
| `lab/reproduce.py` | Nightly step after the analysis: a random sample of cached chunks and folds run again and compared with what is stored (see Reproduce) |
| `lab/metrics.py` | Pure functions behind the derived tables: daily returns, Sharpe, Sortino, drawdown, block-bootstrap intervals, deflated Sharpe with effective trials, overfit probability (CSCV), trades still needed, Monte Carlo, BTC regimes (`regimes()`, the one labeler: trend up, down or flat, volatility terciles of the volatility seen so far, so a day's label never changes) |
| `lab/suggest.py`, `lab/suggest.toml` | After each analysis and walk-forward: promote, watch, demote, retire, fix and retune suggestions from the derived tables, with the thresholds as data (see Suggestions) |
| `lab/confidence.py` | After each analysis and walk-forward: the score and gates that say whether a strategy has earned real money, and the trial register (see Confidence) |
| `lab/families.toml` | By hand: each strategy class's method family (trend, mean-reversion, momentum and the rest) and data inputs, for `/coverage` |
| `lab/volatility.py` | After the derived tables: HAR against a 30-day mean and an EWMA as daily variance forecasts, scored by QLIKE on months before the lockbox. |
| `lab/scorecard.py` | After the mixtures: each strategy's, mixture's and BTC hold's out-of-sample annual return per market climate (BTC trend x volatility tercile) with a 90% interval, from `walkforward_daily`; the "By market climate" table on `/book`. Markets backtests are split by contract category (`lab/markets.toml` [categories]) into `market_category_results`, shown on `/markets/book`. |
| `lab/book.py` | After the score: what each scored strategy might return beside holding BTC, and what each stage costs and can lose. |
| `lab/mixtures.py` | After the book: strategies run together (1/N, inverse volatility, HRP, each with or without a regime gate), weights and gates fit on earlier walk-forward months and scored on the next, every mixture a `trials` row, PBO across the set; "Running bots together" on `/book`. Spec space and budget: `lab/suggest.toml` [mixtures]. |
| `lab/shadow.py` | Analysis step before the research loop: opens, marks and retires the shadow paper accounts (Shadow accounts) |
| `lab/loop.py` | Last step of each analysis, the nightly research loop: which strategies and mixtures entered or left a market climate's passing set, which promote suggestions appeared or went, and monthly retune fit changes, as `research_events`. See Nightly research loop |
| `lab/guard.py` | The live guard every real-money entry point runs, whatever the engine: `GUARD_LIFTED`, stage caps, venue list, ready, the bankroll kill switch. See `docs/live.md` |
| `lab/live.py` | Live Freqtrade bot entry point: always refuses, nothing deploys it. Its config checks sit on `lab/guard.py`. See `docs/live.md` and the operator rules in `docs/operating-rules.md` |
| `lab/ingest.py` | Job run every 5 minutes: every bot's trades (closed and open) and a snapshot (profit, balance, dry-run flag, each open pair's newest candle time) into Postgres |
| `lab/watcher.py` | Daily loss watch: each bot's marked-to-market equity every 60 s into `watch`, and the kill drill. See Watcher |
| `lab/migrate.py`, `lab/migrations/` | Schema: numbered SQL files applied once each, in order, under an advisory lock; runs as the init container of every job |
| `lab/db.py` | The one Postgres connection: `DATABASE_URL` |
| `lab/util.py` | `utc()`, any timestamp (epoch ms, ISO string, datetime) as UTC, and `finite()`, NaN and infinity as null before Postgres or jsonb |
| `web/` | The site (`/accounts` lists every venue in `web/venues.toml` as connected or with the step that connects it): FastAPI and Jinja, read-only, Forgejo sign-in (`web/auth.py`), server-drawn SVG charts (`web/charts.py`), htmx (`web/static/htmx.min.js`, 2.0.4) for the one polled table; the shell (tokens, fonts, grouped sidebar, phone menu) is the dotfiles web-kit, pinned in `web/kit.lock` and fetched into `web/static/kit/` by `web/fetch_kit.py` (the Dockerfile runs it; local dev: run it once), tabs grouped Now, Evidence and Pipeline in `web/nav.py`; pages Overview, Live (every bot's open positions, re-fetched every 30s), bot detail, Strategies, strategy detail, Walk-forward, Suggestions, Confidence, Book, Markets (`/markets`, `/links`, `/markets/book`), Accounts (every broker balance, watch-only wallet and the watcher kill-switch state, read only), Coverage, Guide, Data, Runs; `/guide` is the reading order (see The site) |
| `lab/tests/` | pytest; the DB tests need `DATABASE_URL` pointing at a throwaway database |
| `Dockerfile`, `lab/requirements.txt` | The `trader` image |
| `.github/workflows/image.yml` | CI: build, migrate twice, tests, every strategy loads; on master also push to GHCR and open and merge the home-config digest-bump PR. See CI |
| `ci/bump_home_config.py` | Opens and merges the home-config digest-bump PR after each master build (the deploy). See CI |
| `lab/cohorts.py`, `lab/cohorts.toml` | Picks a dry-run cohort's roster rows from the analysis tables or the best mixture (see Cohorts) |
| `ci/cohort_roster.py` | Run on the fleet machine: opens the home-config PR that seeds a cohort's roster rows (see Cohorts) |
| `ci/research_sync.py` | Run on the fleet machine each morning: files the loop's changes on the agent board. See Nightly research loop |
| `docs/README.md` | Index of the pages in `docs/` |

## The no-real-money guard

`lab/league.py` exits 2 before freqtrade starts if any `FREQTRADE__` env var is set, if the merged config is not `dry_run: true`, if it is not spot, or if the exchange block carries a key, secret, password, uid, private key or wallet address. Public market data needs no keys, so the exchange files keep them empty. `lab/ingest.py` exits 1 if any bot's `show_config` says it is not dry run. `lab/live.py` is the entry point a live bot would use: it refuses while its own guard is on, and nothing deploys it (`docs/live.md`). `lab/tests/test_money.py` proves these refusals, that paper money adds up, and that each outbound client sends only where its money-map row allows.

## Cohorts

A roster row is `name exchange strategy [cohort overlay]`. The cohort is the league the bot reports in; the overlay is an id from `lab/variants.toml` (`base` for the strategy's own settings), and `lab/league.py` layers its config over the venue's file exactly as the backtest does, so a bot runs the setup it was tested under. A 3-column row is cohort `league`, overlay `base`. An overlay id missing from the toml stops the pod, and the guard reads the overlay file with the others, so an overlay can never turn off dry run or add a key. Ingest copies both columns to `bots` on every run, the overview shows one row per cohort plus the total, `/cohorts` lists each cohort's bots with their overlays, and `trader_bot_profit_pct` carries a `cohort` label.

The `aggressive` cohort (`lab/cohorts.toml`) forward-tests near-misses. `python -m lab.cohorts pick aggressive` takes the top 4 strategies by the low end of the 365-day Sharpe interval on the rank venues (binanceus), counting a strategy only with at least 30 trades there, leaves out lookahead-biased or unloadable ones, gives each the setup overlay (or `base`) that scored best for it, and prints one roster row per strategy per venue (4 x 3 = 12 bots; kraken has no backtest history, so it runs the overlay chosen on binanceus). The promote gates are not applied; the backtests it picks from are counted trials, so the search still raises the deflated-Sharpe bar. `ci/cohort_roster.py aggressive` runs on the fleet machine, asks the deployed image for those rows (`kubectl exec deploy/trader-web -- python -m lab.cohorts pick aggressive`) and opens a home-config PR appending them with replicas raised to match. It never merges, and it seeds only a cohort with no roster row yet: growing or replacing a cohort is a person's edit, and grow it only after `TraderBotApiDown` has stayed quiet for a week. `DRY_RUN=1` prints the rows.

The `mixture` cohort forward-tests the best mixture (`lab/mixtures.py`, every one a counted trial). `python -m lab.cohorts pick mixture` takes, among the 1/N ungated mixtures with at least 90 days of out-of-sample return, the one with the highest interval low end, and prints one roster row per sleeve on the sleeve's own exchange, overlay `base`. Only 1/N ungated qualifies because the bots run it as scored: each bot gets the same wallet and trades every day, so there is no weighting and no regime gate. Each sleeve runs its strategy with its own parameters; the walk-forward's tuned ones stay in the backtest. `ci/cohort_roster.py mixture` seeds it the same way as `aggressive`.

The `strategies` cohort runs every strategy in dry run: `python -m lab.cohorts pick strategies` prints one row per loadable, lookahead-clean strategy outside the template, exit-example, biased-control and short families, on binanceus only, overlay `base` (52 bots on 2026-11-09). It refuses a pick whose candle calls (one per pair per new candle, summed over the bots) pass the venue's budget in `[strategies.budget]`, half of binanceus' public rate limit, since every pod shares one address. `ci/cohort_roster.py strategies` seeds it like the others, skipping a row that a bot already runs with the same strategy, exchange and overlay. Cost per bot, why one venue and why not many strategies per bot: [strategy-coverage.md](docs/strategy-coverage.md).

The `winners` cohort is the candidate league: the shadow paper accounts (`lab/shadow.py`, see Shadow accounts) that the nightly rule judged winning. `python -m lab.cohorts pick winners` prints one row per winning account, best forward return first, up to `max_bots` (8), on the account's own venue under its own overlay (a non-base overlay is in the bot name). An overlay no longer in `lab/variants.toml` is left out. A shadow mark is a backtest replayed on candles, so a pod is what adds live fills and fees. `ci/cohort_roster.py winners` opens the home-config PR like the others; a person merges it.

The `markets` cohort is the event-contract paper league (`lab/markets/paper.py`): one paper account per variant of each family in `lab/markets.toml` [paper], with no roster rows. The overview and `/cohorts` show it as one more cohort row (accounts marked of accounts, profit as equity less capital, settled and open fills), and the overview total includes it. It fills only while a CronJob runs `python -m lab.markets.paper` after each recorder sweep (home-config, board row tr-74).

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
  -c 'python lab/migrate.py && python lab/migrate.py | grep -q "0 new" && python -m pytest -q -p no:cacheprovider lab/tests'
docker rm -f trader-pg && docker network rm trader-test
```

A quick analysis of two strategies: run a backfill into the mounted `/lab` first (below), then `python lab/migrate.py && python lab/analyze.py` in the test container with `-e LAB_RESULTS=/lab -e ANALYZE_EXCHANGES=binanceus -e REGIME_EXCHANGE=binanceus -e "ANALYZE_STRATEGIES=Strategy001 SampleStrategy"`. It reads which candles exist from `data_coverage`, so the backfill has to write there too. A venue x timeframe is analyzed only once every whitelisted pair has been backfilled to its oldest candle, so a venue mid-backfill (okx's first pass) is skipped with `not fully backfilled` instead of caching years run on a few pairs. Two strategies over about a year of binanceus data took under a minute.

Analysis knobs (env): `ANALYZE_EXCHANGES` (`binanceus coinbase`), `ANALYZE_STRATEGIES` (empty means all), `ANALYZE_WORKERS` (at most 6 backtests at once), `ANALYZE_MEMORY_GB` (10: the estimated memory of the tasks in flight together; keep it 2 GB under the pod limit), `BIAS_EXCHANGE` (`binanceus`), `LOOKAHEAD_DAYS` (120), `RECURSIVE_DAYS` (30), `REGIME_EXCHANGE` (`coinbase`, the longest BTC history), `CHUNK_TIMEOUT` (3600 s).

Backfill knobs (env): `BACKFILL_EXCHANGES` (`binanceus coinbase kraken`), `TIMEFRAMES` (`1m 5m 15m 30m 1h 4h 1d`, each capped at what the exchange offers), `FORWARD_EXCHANGES` (`kraken`), `PREPEND_FROM` (`20150101`), `PREPEND_FLOORS` (`1m=20240101`: space-separated `<timeframe>=<date>` pairs, the oldest candle a timeframe is prepended to), `HALT_DAYS` (`7`: a run of flat, zero-volume candles at least this long is a suspended market the exchange padded, and is dropped), `LAB_RESULTS` (`/lab`). For a quick local run, add `-e TIMEFRAMES='4h 1d' -v "$HOME/.cache/trader-lab:/lab"` to the test container and run `python lab/backfill.py`.

## CI

`.github/workflows/image.yml` runs on every pull request and on each push to master. `secrets` runs gitleaks v8.28.0 over the commits the PR or push adds and fails on any finding. It does not scan all history: one full scan on 2026-11-06 found 801 hits in 94 commits, all freqtrade API JWTs in logs committed in 2023-2025 and an old `jwt_secret_key` and `ws_token` in old config files, none of them still in HEAD. To repeat that scan:

```sh
docker run --rm -v "$PWD:/repo:ro" zricethezav/gitleaks:v8.28.0 git /repo --redact --no-banner
```

`quality` installs the pinned tools from `lab/requirements-dev.txt`, then runs `npm run typecheck` and `npm run lint`. [package.json](package.json) is a private command manifest with no Node dependencies; Python remains the runtime. [pyproject.toml](pyproject.toml) owns the initial mypy module scope and Ruff rule selection; [standards](docs/standards.md) explains their limits. The image test job reads `scripts.test` from the same manifest and executes it inside the Python image, after migrations.

For static checks in a local Python environment:

```sh
python -m pip install -r lab/requirements-dev.txt
npm run typecheck
npm run lint
```

`npm test` runs pytest using the active Python environment. The Docker commands under [Run locally](#run-locally) supply the runtime dependencies and disposable database for the full suite. An unset `DATABASE_URL` skips database tests.

`secrets`, the migrate-and-test step, the calibration step and the ML test step each stop after 10 minutes (`timeout-minutes`), so a hung test fails the run instead of holding a runner for six hours. `test` builds the image, runs the migrations twice and the tests against a Postgres 17 service; the second run must report `0 new`, which proves a migration applies once and a rerun is a no-op:

```sh
python lab/migrate.py && python lab/migrate.py | grep -q "0 new" && python -m pytest -q -p no:cacheprovider lab/tests
```

It then lists the strategies and fails on a load error or a duplicate name:

```sh
freqtrade list-strategies --recursive-strategy-search \
  --userdir /repo/user_data -c /repo/user_data/config.base.json -c /repo/user_data/exchanges/binanceus.json \
  | tee strategies.txt
! grep -E 'LOAD FAILED|DUPLICATE' strategies.txt
```

`test-ml` builds the ML image (`--build-arg FREQTRADE_IMAGE=$ML_BASE`, the `2026.10_freqai` tag pinned by digest) and runs `lab/tests/test_ml.py` and `lab/tests/test_families.py` there. `test-calibration` runs `lab/tests/test_calibration.py` with `CALIBRATION_E2E=1`, whose end-to-end universe (about three minutes) the `test` suite skips, so it runs beside the suite instead of lengthening it. `publish` runs only on a push to master, after `secrets`, `quality`, `test`, `test-calibration` and `test-ml` pass, and pushes `trader` and `trader-ml` to GHCR as `sha-<7>` and `latest`.

`bump-home-config` then runs `ci/bump_home_config.py`: it sets the `trader` and `trader-ml` digests in home-config `apps/freqtrade/kustomization.yaml` (`images:`) to the one just pushed, opens that change on Forgejo as PR `trader-sha-<7>` through the public API at git.kennethblack.me, closes the older open `trader-sha-*` PRs it supersedes, then squash-merges its own. That merge is the deploy: Flux rolls the bots onto the new digest. It does not wait on home-config's PR checks, which never exercise a digest line; the image already passed `test` and `test-ml`. A bump that cannot merge fails the job and stays open for a person. It skips when master has already moved past the commit (the newer build opens the bump) and warns and skips when the repo secret `FORGEJO_DEPLOY_TOKEN` is unset. That secret is a Forgejo token with write on home-config, the same one the Forgejo deploy workflows use:

```sh
gh secret set FORGEJO_DEPLOY_TOKEN -R Kblack0610/freqtrade_trading_attempt --body "$(rbw get forgejo_deploy)"
```

## The analysis

`lab/analyze.py` runs nightly and is incremental:

- **Chunks.** Each strategy x exchange is backtested per past calendar year, from the first year with candles, then per month of the current year up to today, each chunk from the same 1000 USD wallet. A chunk is keyed by the strategy file's hash, both config files, the freqtrade version, its dates and a digest of its year's or month's candles, so a past year or month runs once and is cached; a normal night redoes only the current month. On January 1 the year just ended runs again as one chunk, and storing a chunk deletes every stored chunk of the series it overlaps, so no day is counted twice. A trade still open at a chunk's end is closed there, so the current year has a forced close at each month end. Changing a strategy or a config reruns all of its years. A failed chunk is retried on the next two nights, then left failed until its key changes. A run is marked failed when more than 10% of its chunks fail.
- **Setup overlays.** `lab/variants.toml` declares account settings to backtest every strategy x exchange under besides its own: each `[overlays.<group>]` is a grid (`lab/grid.py`), and each settings set it expands to is one overlay, named by the first 8 hex digits of its content hash (`overlays` table, `backtest_chunks.overlay`, `''` for as-is). An overlay may set only `max_open_trades`, `stake_amount`, `stoploss`, `minimal_roi` and `pairs` (the first N of the venue's whitelist); anything else, `dry_run` and keys included, fails the run. Its config is layered last over both config files and joins the chunk's cache key, so overlays cache like any chunk and leave the as-is keys unchanged. Overlay chunks run after the as-is ones, only for a strategy x exchange walk-forward has scored (`confidence.score` set), and at most `[sweep] max_chunks` a night: never-run chunks first, the strategy x exchange with the best 365-day Sharpe low end first, then stale ones (each night's current month) the one stored longest ago first, so refreshes rotate through every series; the run's `overlay_chunks_waiting` says how many wait, so a wide grid is walked a slice a night. An overlay series holding an open shadow account runs outside that cap, since its account is marked from the days its current month adds. Each strategy x exchange x overlay gets daily returns in `overlay_daily` and windows in `overlay_windows`, its series counts in every window's deflated Sharpe (as-is ones too), and it is one `trials` row (`overlay:<id>`). The other derived tables, suggestions and confidence read the as-is results only. An overlay taken out of the toml takes its chunks with it; its trials stay.
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
- **Regimes.** Every day gets a BTC trend (up, down or flat, with a 5% band around its 200-day mean) and a volatility third (30-day realized volatility against expanding history, after 90 days), and the strategy page splits returns by both.
- **Drift.** Each league bot's strategy is backtested on its own exchange from the bot's start to now, with the bot's config, and set against its dry-run trades (`drift`). An entry matches when the backtest entered the same pair within one candle; the gap is live minus backtest profit per trade over trades closed on both sides. A low match rate says the backtest does not model what the bot does (startup candles, data gaps, a different pairlist); a steady negative gap is fill or fee error the backtests leave out. It needs weeks of trades to mean anything. The Overview shows the match per bot and each bot page has the comparison. `reconcile` keeps the same match trade by trade, split the way implementation shortfall splits it (Perold): entry delay, price paid over the backtest's, fee, and the fills the bot missed (`bt_only`).
- **Markouts.** `lab/markouts.py` marks every paper entry at +5m, +1h and +4h after its fill (`[markouts]` in `lab/suggest.toml`): the move signed by side, minus the pair's BTC beta times BTC's move, minus the round-trip fee, beside the same figure for random entries on the same pair and side within a day (`markouts`). This is closing line value from sports betting: an entry with skill beats the null in tens of trades where profit needs thousands. A diagnostic only; making it a gate is the human's call.
- **Correlations.** Each strategy's daily returns against every strategy the league runs on that exchange, over the last 365 days; near 1 means it would only duplicate a bot already running.

The leaderboard ranks by the low end of the Sharpe interval, so a strategy has to look good on its unlucky draw. Rows under 20 trades sink, and lookahead-biased rows go last. Promoting a strategy is a home-config roster PR that the nightly research loop opens and a person merges.

## Walk-forward

A backtest of a strategy as it is says nothing about tuning it, and hyperopt's best result on a span is fitted to that span. `lab/walkforward.py` runs nightly to measure how much of hyperopt's gain survives on data it never saw: Each test-month trade, dated and purged, is also kept in `walkforward_trades` (variant `base` or `tuned`), with `walkforward_daily` summing profit ratio per UTC close day: the dated out-of-sample series.

- **Candidates.** The league's strategies, then the promote suggestions, then the watch ones by score, then the best 365-day expectancy low end on each venue in `WF_EXCHANGES`, taking one venue's next best after another, all loadable and lookahead-clean, up to `WF_LIMIT`. coinbase is not a walk-forward venue: its 2.4% round trip leaves nothing to tune.
- **Venues.** Every candidate runs on every venue in `WF_EXCHANGES`, one fold row per venue, once that venue's candles for its timeframe are fully backfilled (the same coverage guard as the analysis). A fold whose training span starts before the venue's first candle is skipped, and the run's `skipped` list says why.
- **Memory.** Folds share `WF_MEMORY_GB` by their estimated size (training days plus a month of candles across the venue's pairs, at twice a backtest's load), so a 22-pair venue does not start ten hyperopts at once.
- **Folds.** For each whole calendar month before the lockbox whose `WF_TRAIN_DAYS` training span starts on or after the venue's first candle (about 60 to 80 months on venues with candles from 2019; `WF_FOLDS` caps it to the newest few), hyperopt tunes the strategy on the `WF_TRAIN_DAYS` before it (`--spaces default`: ROI, stoploss, trailing, max open trades and any declared buy and sell parameters) with a random state seeded by strategy and month, so a rerun gives the same answer. Then the month is backtested as is and with the tuned parameters, and the training span is backtested with them too (in sample). Every per-trade profit ratio is kept.
- **Purge and embargo.** Training stops `WF_EMBARGO_DAYS` short of the test month, so no training trade is still open when the test begins. A trade the backtest force-closes at the end of a span (the training span or the month) straddles a fold boundary and is dropped from that span's trades and return. Changing the embargo reruns every fold.
- **Shards.** The job is an Indexed Job of `WF_SHARDS` pods, each running the strategy x venue pairs that hash to its index (`JOB_COMPLETION_INDEX`), so the pods plan the same split without talking. Each records a `walkforward_shard` run row named `WF_JOB#index`; the last to finish, under a Postgres advisory lock, runs the post steps (suggest, confidence, book, mixtures, scorecard, horizon) once and records the night's `walkforward` row with the shards' counts summed. A shard killed before it records leaves the night without that row, and its folds run the next night.
- **Candles.** The pods run on any amd64 node, so they do not mount the lab PVC: an init container runs `lab/snapshot.py fetch` to copy the latest candle snapshot from MinIO to the pod's disk. `lab/snapshot.py publish` (CronJob `freqtrade-candles-publish`, 00:45, on the PVC's node) uploads each changed candle file by its sha256 plus a manifest and keeps two manifests. The copy is byte for byte, so the fold cache keys do not change.
- **Isolation.** Hyperopt works on a copy of the strategy's folder in a temporary userdir, because it writes its params file next to the strategy, where every nightly backtest would load it.
- **Cache.** A fold is keyed by the strategy file, both configs, the freqtrade version, its dates, the hyperopt settings and a digest of its candles from training start to test end, so a normal night skips stored folds, and a changed file reruns all of that strategy's months. Failures retry like analysis chunks; a month with no candles is stored once. A fold where no epoch reached `WF_MIN_TRADES` training trades is kept, untuned, with that as its note.
- **Reading it.** The Walk-forward page pools each strategy's months: how many the tuned parameters beat, the per-trade profit as is and tuned with 90% bootstrap intervals, and walk-forward efficiency, the tuned return per day out of sample over the same in sample. Near 1 the tuning carries over; near 0 or below, hyperopt fitted noise. The strategy page has the months one by one.

| Env | Default | What it is |
|---|---|---|
| `WF_EXCHANGES` | `binanceus` | Space-separated venues folds run on |
| `WF_FOLDS` | `0` | Test months, the newest whole ones; 0 reaches back to each venue's first `WF_TRAIN_DAYS` of candles |
| `WF_TRAIN_DAYS` | `180` | Training span before each test month |
| `WF_EMBARGO_DAYS` | `7` | Days between the end of training and the test month; longer than any trade's hold |
| `WF_EPOCHS` | `150` | Hyperopt epochs per fold |
| `WF_EPOCHS_ROI` | `60` | Hyperopt epochs for a strategy with no parameters of its own (ROI and stoploss only) |
| `WF_EARLY_STOP` | `40` | Stop hyperopt after this many epochs without a better loss; 0 turns it off, and freqtrade raises a value under 20 to 20 |
| `WF_SAMPLERS` | `NSGAIIISampler TPESampler` | Optuna sampler arms; each fold takes one by its seed (the A/B in the run report's `arms`) |
| `WF_LOSS` | `SharpeHyperOptLossDaily` | Hyperopt loss |
| `WF_MIN_TRADES` | `10` | Training trades an epoch needs to count |
| `WF_LIMIT` | `16` | Candidates per run |
| `WF_WORKERS` | `6` | Folds at once; each hyperopt runs on one core (`-j 1`) |
| `WF_TIMEOUT` | `10800` | Seconds before one hyperopt is killed |
| `WF_BUDGET_S` | `0` | Seconds after which a run starts no more folds (0: no limit). Keep it under the job's deadline by `WF_TIMEOUT` plus the post steps; the run records `folds_left` and the next one carries on by cache key |
| `WF_MEMORY_GB` | `10` | Estimated memory the folds running at once may hold |
| `WF_STRATEGIES` | all | Space-separated names to limit a run to |

## Calibration

The confidence gates are thresholds chosen by hand, and their error rates were never measured on the pipeline as built. `lab/calibration.py` (weekly, the `freqtrade-calibration` CronJob) measures them. It writes seeded synthetic candles (GARCH(1,1) volatility, Student-t returns, and a persistent signal in the volume column that predicts the next hour's return with correlation `signal_ic`), and strategies that read that signal mixed with their own noise, so each one's correlation with the next return is one of `edges`, or zero for a null, which enters at random. Each has two tuned thresholds, so hyperopt has something to overfit.

They run through the real `lab/analyze.py`, `lab/walkforward.py` and `confidence.rows()` in the Postgres schema `calibration_scratch`, with a temporary repo holding only the synthetic venue, so nothing reaches the trial register, the site or the league. The deflated Sharpe and minimum backtest length gates count the production register, the burden a real strategy faces. A universe (one seed, tagged with its number and the ISO week it started, `0001-2026-W46`) can take several runs: the walk-forward stops at `WF_BUDGET_S` and the next run carries on from its cached folds. When none are left, each strategy's gates over its newest 12 and 60 test months go to `calibration_outcomes`, `calibration` is rebuilt over every universe so far (rate and Wilson 95% interval per gate, edge and length; gate `stack` is all research gates together), and the next run starts a new universe. The paper gates (dry run, drift, not decayed) and venues agreeing need paper bots or a second venue and stay outside `stack`.

Every run also stores SPA and StepM (Hansen 2005, Romano and Wolf 2005, via `arch`) in its `runs` row: whether any strategy's as-is daily returns over the year before the lockbox beat cash, per venue, and which ones. A diagnostic, not a gate.

Settings: `[calibration]` in `lab/suggest.toml` (pairs, test lengths, volatility, signal, edges, strategies per edge, nulls). `CALIBRATION_SCHEMA` and `CALIBRATION_WORK` (where the candles are written, about 0.2 GB per pair) default to `calibration_scratch` and the temp directory; the `WF_*` settings reach the walk-forward as in its nightly job. A planted edge shows the pipeline finds a clean edge and invents none; synthetic candles say nothing about whether crypto has one.

## Reproduce

A cached result is trusted because its key still matches, which holds only if the same inputs give the same output. `lab/reproduce.py` checks that every night, as the last step of the analysis job: it takes `REPRO_CHUNKS` (3) backtest chunks and `REPRO_FOLDS` (1) walk-forward folds at random among those whose stored cache key equals the key their inputs give now, runs them again, and compares trades and final balance (chunks) or tuned parameters and trade counts and returns (folds). Its `runs` row (kind `reproduce`) lists each result and any difference; the exporter publishes `trader_repro_checked` and `trader_repro_mismatch`, and home-config alerts `TraderReproMismatch` on any mismatch. A rerun that fails makes the run fail (`TraderRunFailed`) without counting as checked.

Every chunk and fold also stores its provenance: `revision` (the commit the image was built from; GHCR tags it `sha-<7>`, which names the digest home-config pinned), `config_digest`, `candles` (the digest in its key) and, for a fold, its hyperopt `seed`. `lab/tests/test_golden.py` is the proof underneath: one strategy backtested twice on fixed candles gives the same trades byte for byte, and one changed candle changes its digest, the trades, and what the reproduce check reports.

## Suggestions

`lab/suggest.py` runs at the end of every analysis (alone: `python lab/suggest.py`) and rebuilds `suggestions` from the derived tables. Every threshold is in `lab/suggest.toml`; each suggestion stores every gate it was held to with the value seen, so the site shows why.

- **Promote**: a strategy and exchange the league does not run yet that is lookahead-clean, has at least 30 trades in the last 365 days, has the low ends of its expectancy and Sharpe intervals above zero, a deflated Sharpe of at least 0.90, at least 70% of at least two full calendar years profitable, a drawdown under 35%, daily returns that do not track a league strategy (|rho| at most 0.70), the trades and expectancy gates met on at least two venues (`min_venues`), so an edge on one thin book stays a watch, and an overfit probability on its venue of at most 0.5 (`max_pbo`). The action is the exact `roster.tsv` line and the new replica count.
- **Watch**: lookahead-clean candidates one gate short, best Sharpe interval first, so a near miss is visible before it qualifies.
- **Demote**: a league bot whose own dry run lost money over 60 days with at least 20 trades, whose strategy's 365-day backtests on that exchange lose even at the high end of the expectancy interval (kraken has no history, so for its bots every research exchange must agree), or whose strategy has lookahead bias. Rows map to pod ordinals and each pod keeps its own sqlite, so the action says to replace the row in place and clear that pod's PVC, never to delete the row.
- **Retire**: lookahead bias or a load error, which supersedes a fix for the same file; or a decayed bot (see Decay test), written by `lab/confidence.py` with the bot named.
- **Fix**: the recursive check found an indicator still moving at the startup candle count, or could not run because the strategy declares none.
- **Retune**: a lookahead-clean strategy whose tuned parameters beat it as it is on each of the newest two walk-forward months (at least 5 tuned trades each), and, pooled over every month, has at least 30 tuned trades, the low end of the 90% interval of its per-trade gain over as is above zero (so a tie by luck does not pass), and the low end of its own 90% interval above zero. The action is the newest fold's params file, to save next to the strategy.

A suggestion keeps its `first_seen` while it holds, so "since Nov 05" on the site is how long the evidence has pointed the same way; one that stops applying is deleted.

## Stress gates

Confidence requires seven stress checks. A slippage sweep subtracts each additional round-trip cost in `[confidence] slippage_sweep` from the out-of-sample expectancy interval. Trade reordering must stay inside the hard stop at Stage 1 stake; pair holdout must remain positive without the most profitable pair, using tuned walk-forward trades before the lockbox; venue agreement requires positive tuned expectancy on at least two venues. These reuse trades and add no trials.

`lab/robustness.py` also replays each tuned walk-forward test month with delayed entry signals, one numeric buy/sell parameter nudged at a time in both directions, and `--timeframe-detail` using `[confidence] detail_timeframe` candles. Each scenario pools its own months and needs at least `min_member_trades` trades with its bootstrap expectancy interval low above zero. Scenarios never tune on the test month. Missing, failed, unsupported, stale or empty evidence blocks readiness; a valid baseline remains usable for scoring. Strategies without numeric buy/sell parameters cannot clear parameter nudges. A requested nudge outside declared bounds or decimal precision also blocks the gate; values are never clamped back to the baseline. A strategy at or below the detail timeframe cannot clear the detail gate with that setting.

Walk-forward stores scenarios in `walkforward.robustness`; their specification and tuned parameters enter the persistent trial register. Its cache includes experiment settings, implementation and finer-candle digests, so existing folds rerun through the normal budget. The confidence page displays every gate and its evidence. To populate the new evidence, run the usual walk-forward job with finer candles present for every whitelisted pair over the test months, then rescore confidence. No gate authorizes a bot or changes live-entry controls.

| Symptom | Check and action |
|---|---|
| Entry delay, parameter nudges or timeframe detail says missing evidence | Run walk-forward after migration 038; older folds have no scenario evidence. |
| Timeframe detail cannot run | Backfill the configured smaller timeframe for every venue pair and test month, then rerun walk-forward. |
| Parameter nudge is unsupported | Inspect the strategy's tuned buy/sell parameters and declared bounds; unsupported experiments block readiness. |


## Book

`lab/book.py` runs after the score (alone: `python lab/book.py`) and rebuilds `book`, shown at `/book`. Each scored strategy with enough walk-forward months gets a 5th, 50th and 95th percentile return over 1 and 5 years, from a block bootstrap of its out-of-sample monthly wallet returns, and the same for BTC over those months. With no score there is no number: the page says there is not enough evidence. The ranges assume the tested months repeat, so a short basis makes a 5-year range look firmer than it is. The page also lists each stage's capital, what it can lose at each limit and its running costs, from `lab/suggest.toml` [book].

`lab/mixtures.py` runs after the book (alone: `python -m lab.mixtures`) and rebuilds `mixtures` and `mixture_daily`. A mixture is a set of sleeves (one strategy on one exchange, its daily return the walk-forward test months' trades from `walkforward_daily`), a weighting (1/N, inverse volatility, or hierarchical risk parity) and an optional regime gate, which keeps a sleeve in cash on days whose climate lost it money in training. For each month the sleeves share, after the first `min_train_months`, weights and gates are fit on the months before it and scored on it; `fit()` refuses a training day inside the month it sizes. Each mixture is a `trials` row (strategy `mixture`, variant the md5 of its spec), so every spec searched raises the deflated Sharpe bar, and the overfit probability is computed across the set. The specs come in a fixed order cut at `budget`, so a rerun on the same pool adds no trials. The 1/N mixture of the best-scored pool members, ungated, is the baseline. The climate scorecard splits each mixture by climate like a strategy. Markets backtests store no dated returns yet, so they cannot be sleeves until they do.

Analysis also fills `benchmarks` from the regime venue's daily closes: holding BTC, holding BTC and ETH, monthly DCA into BTC, and BTC only above its 200-day mean, each over the last 1, 3 and 5 years and the whole shared history. Tax rates there are guesses, in config.

Analysis also scores a volatility forecast (`lab/volatility.py`, knobs in `lab/suggest.toml` [volatility]): per analysis venue and whitelisted pair, daily realized variance from 5m candles, forecast the day before by HAR (log variance on its 1, 7 and 30-day means, refit each month on the days before it) and by two plain baselines, a 30-day mean and an EWMA. Each is scored by QLIKE on every month before the lockbox into `vol_scores`; `vol_verdict` says per venue whether HAR beats both, by a block-bootstrap interval on the daily QLIKE difference that has to sit below zero. `/book` shows the verdict.

`vol_forecasts` keeps each daily HAR forecast and a monthly eligibility flag: before the month starts, at least `[volatility] target_min_score_days` prior scored days must beat both baselines by the same interval test. `lab/vol_target.py` sizes a research sleeve from the tuned OOS trades by `target_annual / sqrt(365 * forecast variance)` on the UTC open day, capped at `target_max_scale` (at most 1). It stores the scaled returns as `walkforward_trades.variant = har_target`, so the climate and horizon tables compare it beside tuned. A missing or ineligible forecast allocates zero; its zero-return source decision stays in the counts, and the run records active and missing-forecast counts. Each strategy, venue and sizing specification enters `trials`, including cash-only results. These are research allocations applied to fixed trade signals; sizing does not rerun exits or model wallet constraints. No bot consumes them. Mixture inverse-vol weights still use training sleeve returns; `[mixtures] variant` can select the HAR sleeve for a separate mixture experiment.

## Nightly research loop

On razer-laptop, walk-forward (01:00 UTC) adds folds it has not stored and analysis (02:45) rescores everything: suggestions, confidence, book, mixtures, the climate scorecard. The jobs can overlap; each scores the folds available at its own finish, so analysis can publish before that night's new folds arrive. Analysis's last step, `lab/loop.py` (alone: `python -m lab.loop`), compares two sets with the previous run's, kept in `research_state`: every strategy or mixture whose climate cell has its low end above zero, and every open promote suggestion. Each member that entered or left becomes a `research_events` row; `trader_climate_passing{regime,kind}` counts the first set. The first run has no previous set, so everything passing enters once.

`ci/research_sync.py` runs at 10:00 UTC on the fleet machine (agentctl runner `trader-research-sync`, from origin/master). It reads the unreported events through the deployed image (`kubectl exec deploy/trader-web -- python -m lab.loop pending`, the site's read-only role) and marks them reported in `deploy/freqtrade-watcher`, which holds the writer role, files one agent-board row per night for the climate changes, and one board row for each promotion that enters or leaves. It opens no roster PR: forward testing is the shadow paper accounts (Shadow accounts below), and a pod is a person's pick through `ci/cohort_roster.py`. Monthly retuning reuses the fits produced by walk-forward and the gates in `[retune]`. Each qualifying retune suggestion joins `research_state`, identified by a digest of its action (training dates and parameter JSON). A new monthly fit or changed parameters enters once; a superseded fit or one that loses qualification leaves once. The sync files a review row carrying the parameter artifact for each change. It never saves parameters beside a strategy or applies them to a bot. An unchanged fit stays quiet, including while the fixed lockbox prevents newer training months. Each event is marked reported (`python -m lab.loop ack <id>...`) once its row is filed, so a failed morning retries the rest. `DRY_RUN=1` prints what it would file.

### Shadow accounts

`lab/shadow.py` runs in each analysis after horizon and before the research loop. A series, a strategy x venue as it stands or under one setup overlay, opens a shadow paper account, a `shadow_accounts` row with paper capital and no pod, when its 365-day backtest expectancy interval and its walk-forward out-of-sample mean profit per trade are both above zero on at least 30 trades each, it never had an account, and its BTC-residual daily returns correlate at |rho| 0.5 or under with every active account of its cohort; best Sharpe low end first, until the cohort is full. Short holds the median trade under a day, long a day or more, 60 accounts each. An overlay series is keyed by its overlay id (`shadow_accounts.overlay`, account `shadow-<strategy>-<venue>-<overlay>`), is gated on its own 365-day window and its base's walk-forward edge (walk-forward scores only the strategy as it stands), and meets the same diversity bar, so the `lab/variants.toml` grid is what the loop spawns from. Each night appends `shadow_marks`, one per day after the promotion day from `strategy_daily` (`overlay_daily` for an overlay series) through the last day its backtest covers: days replayed on candles that nobody had seen at promotion, never rewritten. The rule: retire past the cohort's drawdown (20% short, 30% long), when the block-bootstrap interval of the mean daily return is below zero after the minimum days (30 short, 90 long), or at the day limit (60, 180) without winning; winning when up with the interval's low end above zero. A retired series never reopens (an overlay of it still can), and its slot is refilled the same night. All of it is `lab/suggest.toml [shadow]`; `/accounts` ranks the accounts, `trader_shadow_accounts{cohort,state}` counts them, and home-config's `TraderShadowMarksStale` warns when the newest mark passes 50 hours while an account is open. Paper only: nothing here trades or edits the roster; the winners reach a pod only through the `winners` cohort (Cohorts) and a person's merge.

## Confidence

`lab/confidence.py` runs after suggestions (alone: `python lab/confidence.py`) and rebuilds `confidence`, one row per strategy and exchange, shown on `/confidence`. Promote decides who enters the dry-run league; this decides who could touch money, so its gates are stricter. It is advice: nothing here touches a bot, and `lab/league.py` still refuses real money.

- **Score**: 100 x P(true net expectancy per trade > 0). Each strategy's pooled tuned walk-forward trades, shrunk toward every strategy walk-forward has tried. The population mean is capped at zero, so a rising market does not lift everyone. No score below 20 strategies with 30 trades each. Until then `/confidence` and `/book` show how many count so far and each strategy's raw out-of-sample average per trade, marked unscored.
- **Ready**: every gate passes and the score is at least 90. A failed gate shows as "not ready: <gate>" whatever the score. Ready means worth a small capped experiment, not safe.
- **Gates**, thresholds in `lab/suggest.toml` [confidence]: bias, costs at the venue's configured fee, 250 out-of-sample trades, expectancy interval low above zero, deflated Sharpe, overfit probability, minimum backtest length (the tuned out-of-sample years must reach `min_btl_ratio` x 2 ln(N) / SR^2, Bailey et al 2014 theorem 3.1, N the register's size and SR the annualized Sharpe of those days), months positive, BTC regime spread, excess over hold, worst open loss, cost stress, drift, dry run length, not decayed, score.
- **Out of sample** means the tuned returns, months hyperopt never saw. So the score speaks for tuning monthly and trading the next month, not for the file as it stands, and candidates were chosen on a trailing year that overlaps those months.
- **Lockbox**: months from `[lockbox] start` on stay out of walk-forward, retune, the score, the book, mixtures, the climate scorecard and the horizon verdict; a fold stored before the box existed is dropped by every reader (`test_no_reader_takes_a_stored_fold_whose_test_month_is_in_the_lockbox`). The date is fixed, so the box grows until a strategy clears everything else. Then it gets one look: a PR moves the date, and a migration in that PR adds the look to `trials`. Two limits: `strategy_windows` still covers those months, so the gates reading it (overfit, excess over hold) have seen them, and the candidates were chosen on a trailing year that overlaps the box. Folds stored before the lockbox stay on `/walkforward`.
- **Decay test**: retiring is separate from the hard stop. The hard stop (`[book] hard_stop`, `lab/watcher.py`) acts on capital falling from its peak and pauses a bot; the decay test asks whether live trades are still drawn from what walk-forward measured. It is a one-sided CUSUM (Page 1954) of the log-likelihood ratio, per live closed trade in close order, of "mean is zero" against "mean is the low end of the out-of-sample 95% interval", with the pooled out-of-sample standard deviation. It floors at zero, so old good trades bank nothing, and alarms at ln(`decay_false_alarm_trades`) (Lorden's bound: if the edge is at least that low end, a false alarm takes that many trades on average). One bad week cannot cross it; a sustained run below the low end does. A crossing is the "not decayed" gate failing and a `retire` suggestion for each bot on the strategy and exchange; nothing is tested for a strategy with no positive low end or no live trade. A deep drawdown inside the strategy's out-of-sample range does not retire it.
- **Stop rule**, written before the Saturday 2026-11-07 run: if no strategy reaches ready after that run and the remaining stress tests, the recommendation is core hold only and the live work ends. Results do not move a threshold afterwards.
- **Trial register**: `trials` counts every strategy, exchange, setup overlay and tuned parameter set ever tried, and keeps a row when its strategy goes, so the bar never falls. The deflated Sharpe uses that count at the window's effective share.

## Adding a strategy

Drop the `.py` in `user_data/strategies/` (any subfolder). It must import on the pinned image: Python 3.14, NumPy 2 (`np.nan`, not `np.NaN`), no `ta` package. Check with `freqtrade list-strategies --recursive-strategy-search --userdir user_data -c user_data/config.base.json -c user_data/exchanges/binanceus.json`. The next nightly analysis backtests its whole history and checks it for bias. Give its class a line in `lab/families.toml`, or `/coverage` lists it as untagged and the tests fail.

## Generated families

`user_data/strategies/candles/` holds 47 fixed-exit strategies, one per TA-Lib candlestick pattern that can signal bullish. The 14 bearish-only functions listed in its `family.toml` cannot enter a spot long and remain excluded. Every successfully backtested strategy and venue enters `trials`, including a variant with zero trades. This is paper research after the Saturday stop-rule read; the core-hold recommendation and live guard still apply.

`user_data/strategies/chart/` holds five fixed-exit long strategies from `lab/patterns.py`: resistance break, double bottom, ascending triangle, symmetric triangle and bull flag. The numeric detector also covers support breaks, double tops, downward triangles and bear flags; these have no spot short strategy. Each active candle or chart variant is classified in `lab/families.toml` for `/coverage`, backtested by the next nightly analysis, and counted through the same trial register.

Five more generated families are scanned, each signal in `lab/channels.py`: `trend/`, a long-flat Donchian ensemble (each lookback goes long on a close above its highest close and flat on one below the lowest of half as many bars, the score is the share long, and `custom_stake_amount` scales the stake by that score and by a volatility target), for the up and down climates; `range/`, a buy of a close two deviations under its mean, only while BTC's trend is flat by `metrics.trend_buckets` (band from `[confidence] trend_band`), sold back at the mean, for the flat climate; `breakout/`, volatility breakouts (a close a set number of ATRs over the last, over its Keltner channel, or out of a Bollinger-in-Keltner squeeze); `clock/`, an entry at a set UTC hour or weekday held to a time stop; and `flow/`, volume leading price (a surge on an up close, on-balance volume at a new high before the close). Each family shares one exit, and its `family.toml` holds that exit and every parameter. The strategy files are generated from it: edit the toml, `lab/patterns.py` or `lab/channels.py`, then run `python lab/families/generate.py`; `lab/tests/test_families.py` fails while a file is stale. Each family's `ladder` runs every strategy on 15m, 1h, 4h and 1d, since holding period is a diversity axis (docs/strategy-coverage.md): the family's own timeframe keeps the plain name and its cached results, and each other rung is `<Name>_<timeframe>`, its hold times scaled with the candle length and its stop and targets with the square root. The candlestick family stays on 1h: it is already 47 of the scanned strategies. CI checks that every rung loads and that one strategy of each family passes the lookahead check on synthetic prices. The nightly lookahead check covers at least `LOOKAHEAD_CANDLES` (900) candles, so a 1d strategy gets 900 days rather than `LOOKAHEAD_DAYS` (120). A family can still be staged in `lab/families/<family>/`, where it loads and is checked but adds no trials; switching it on is a PR that moves the folder to `user_data/strategies/<family>/`.

`user_data/strategies/ml/` is the machine-learning family, hand-written rather than generated: `MLLightGBMBaseline`, a FreqAI LightGBM regressor that predicts the mean close over the next 24 hourly candles and goes long when the prediction clears 1%, with its FreqAI settings in `freqai.json` beside it (30 training days, 7-day windows, `n_jobs` 2: LightGBM's default of one thread per core thrashed for 90 seconds per fit on a loaded hp-victus, against under 5 seconds pinned). It needs lightgbm, which only the ML image has: `ghcr.io/kblack0610/trader-ml`, this same Dockerfile built with `--build-arg FREQTRADE_IMAGE=$ML_BASE`, the `2026.10_freqai` tag pinned by digest (2.7 GB against 1.3 GB), so the bots never run on it. CI builds it and runs `lab/tests/test_ml.py` there, which checks that each 7-day window is predicted only by a model fit before it; the plain image skips that test. `lab/analyze.py` treats any strategy with a `freqai.json` beside it this way: it backtests it as-is (no overlays) over the last `ML_DAYS` (365) from the first of that month, so its FreqAI training windows move monthly rather than nightly, with that file layered as config and the identifier `<strategy>-<venue>-<chunk start>-<digest of file and settings>`, so models under `user_data/models/<identifier>` are reused only by the chunk they were trained for. A chunk whose start has left the window is deleted. On an image without lightgbm it is recorded with load error `needs the trader-ml image`, which counts as a load-failed trial. Walk-forward leaves it out: hyperopt over a fold's training span would fit a model per pair for every week in it. It gets no bias check yet, so its `lookahead clean` gate reads not checked and it cannot be promoted.

## Price data

`lab/backfill.py` owns the candles; nothing else downloads them. Per exchange and timeframe:

- A series that has not reached the exchange's oldest candle gets a `download-data --prepend --timerange 20150101-` pass. One call reaches the listing date (binanceus from 2019, coinbase from 2015 for BTC). 1m stops at its `PREPEND_FLOORS` date, 2024-01-01: the 1m files are about 30 times the 30m ones, and only a few scalping strategies (BinHV45, CCIStrategy, Low_BB, ReinforcedSmoothScalp, Scalp, SmoothScalp) trade on 1m.
- Then every series gets an append pass up to now. A prepend drops a series' newest candle, so the append always runs after it.
- When a prepend leaves the first candle where it was, the series is marked `oldest_reached` and only appended from then on.
- Kraken serves no OHLCV history, only its newest 720 candles per timeframe. It is collected forward through ccxt and merged into the same Freqtrade files. A run at least every 60 hours keeps 5m gap-free; a longer outage leaves a gap that `data_coverage` counts. These candles exist nowhere else until the next quarterly archive.
- Kraken's past comes from its published OHLCVT archive instead (`lab/archive.py`): the first backfill reads each series from the five-part zip by HTTP range, so the 10 GB file is never downloaded or unpacked, and marks it `oldest_reached`. A failed read is retried next hour and does not stop collection. The archive leaves out candles with no trades, so old thin periods count as gaps in `data_coverage`; Freqtrade fills those on load. It has no 30m, which is built from 15m, and 1m keeps the `PREPEND_FLOORS` date.
- OKX 1m comes from its published monthly files (`archive.okx_candles`, the `market-data-history` listing, no key): a series with no candles yet reads one zip per month from the 1m floor, then the prepend and append passes page only what is left (the current month). The API's 100 candles a call would take about 25 hours for 22 pairs; the files take minutes. A failed read only costs time, since the prepend pages the same history.
- A timeframe the exchange does not serve is built from the longest one it does that divides it: coinbase has no 4h, so 4h is resampled from 1h after each run (the last, unfinished bucket left out) and is `oldest_reached` when its 1h is. freqtrade backtests it like any other file; a live coinbase bot still cannot run 4h.
- The full archive ends at 2026-09-30, so a hole runs from there to the first collected candle of each timeframe. Kraken publishes each quarter after it ends: once `Kraken_OHLCVT_2026Q4.zip` is out, run `python lab/archive.py <url>` once in a backfill-mounted pod, between backfills, to close it.

### Venues

| Venue | Pairs | Fee per side in backtests | Used by |
|---|---|---|---|
| binanceus | 10 USD | ccxt's default | league bots, research |
| coinbase | 10 USD | ccxt's default | league bots, research |
| kraken | 10 USD | ccxt's default | league bots (forward candles only) |
| okx | 22 USDT | 0.20%: 0.15% taker plus 0.05% slippage | research only, no bot |
| kucoin | 18 USDT | 0.15%: 0.10% taker plus 0.05% slippage | research only, no bot |

binanceus has no USD pair trading $5M a day, so its backtests assume fills its books may not give; okx and kucoin add pairs that trade millions a day, where a backtest's fills are believable. Their whitelists come from `python lab/pairs.py <exchange>`: pairs over $2M a day both for the last 30 days and for the 30 days from 2022-01-01, with a daily candle by then, at most 40. Their `fee` key is the stressed cost, set by hand in the exchange file; Freqtrade applies a `fee` key to dry-run fills too, which is why the bot venues do not get one here.

**Survivorship.** Those whitelists are today's liquid survivors. A coin that was liquid in 2022 and died since is not in them, so the research venues' early years flatter every strategy a little. Freqtrade cannot backtest a point-in-time volume list, so every list `lab/pairs.py` writes is recorded as `current-listing` in `user_data/universe.json` (source, date, floor, per-pair volume then and now); a whitelist with no record, or edited since its record, counts as `current-listing` too. Each analysis and walk-forward run stores that record per venue in `runs.raw.universe`, and `/book` shows a "Survivors only" notice for every venue of the latest analysis that is not `point-in-time`, with the pairs, floor and pick date: read those results as an upper bound and look for agreement with a venue not marked. A venue becomes `point-in-time` only when a backtester that takes a per-year pair list exists and `pairs.py` writes that source.

The research venues are backfilled by their own job (1m listed last, so the other timeframes append first), so their slow first pass never holds back the hourly candles of the bot venues. OKX serves 100 candles per request, so its first pass of 5m and up took several runs, each keeping the pairs it finished; its 1m comes from the monthly files above. KuCoin serves 1500 per call and pages its 1m in about 25 minutes.

**Perpetual swaps.** `lab/futures.py` collects funding rates and daily open interest for the okx and kucoin swap of each whitelisted pair through ccxt (Freqtrade cannot download kucoin futures) into `funding_rates` and `open_interest`, and is their only writer. Free history is short: okx serves funding from about three months back and open interest from 180 days; kucoin serves funding from 2024 (`FUNDING_FROM`, the first run's floor) and open interest from about 98 days. So collection runs daily and the rows are kept forever. Each run resumes funding from the newest stored row; open interest is refetched over its window, so a missed day is refilled. Knobs: `FUTURES_EXCHANGES` (`okx kucoin`), `FUNDING_FROM` (`2024-01-01`). The first run took about two minutes, later ones about 30 seconds.

The first run fetches full history (5m since 2019 on binanceus is the long pole) and can take hours; later runs take under a minute. Exchanges run in parallel, each with its own rate limit.

## The database

- `001`: `bots`, `bot_trades` (one row per trade, keyed by bot, trade id and open date, so a bot whose sqlite is lost keeps its old trades), `bot_snapshots` (one row per bot per ingest; rows with `source = 'report'` came from the retired daily report; each ingest thins it to every row for 7 days, the last of each hour to 90 days and the last of each UTC day after that, and never prunes a row showing a bot was not dry run), `runs`.
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
- `021`: `resolutions.volume` (each settled market's lifetime volume) and `market_backfill` (progress of each history job, per venue and unit), written by `lab/markets/history.py`.
- `023`: `paper_accounts` and `paper_orders` (the event-contract paper league, written by `lab/markets/paper.py`) and `demo_orders` (each drill order, written by `lab/markets/kalshi_trade.py`, `lab/markets/manifold_trade.py` and `lab/broker.py`).
- `025`: `broker_balances`, each read of a broker paper account, written by `lab/broker.py`. Its drill writes to `demo_orders`.
- `026`: `broker_balances.paper`, since the table now also holds read-only reads of real accounts.
- `029`: `vol_scores` and `vol_verdict`, the volatility forecast's QLIKE per venue, pair, model and month, and whether HAR beats each baseline, rebuilt by `lab/volatility.py` on every analysis.
- `031`: `mixtures` and `mixture_daily`, written by `lab/mixtures.py`; drops `portfolio` (016), the equal-weight pick on in-sample backtest returns it replaces.
- `033`: `research_state` and `research_events`, the nightly research loop's passing sets and their changes, written by `lab/loop.py` and marked reported by `ci/research_sync.py`.
- `034`: provenance on `backtest_chunks` and `walkforward`: `revision`, `config_digest`, `candles`, and a fold's `seed` (see Reproduce).
- `037`: `vol_forecasts`, daily HAR variance and eligibility from prior months; permits the `har_target` research sleeve in the dated trade store.
- `038`: `walkforward.robustness`, scenario outcomes and their specification for the confidence stress gates.
- `039`: permits monthly retune fits in the research loop state and event stream.
- `045`: `shadow_accounts` and `shadow_marks`, the shadow paper accounts and their appended daily marks, written by `lab/shadow.py` (Shadow accounts).
- `046`: `overlay_daily`, each strategy x exchange x overlay's daily returns, rebuilt by the analysis like `strategy_daily`; overlay shadow accounts are marked from it.
- `047`: the money layer. `fills`, a read-only view of what traded on every engine (`bot_trades`, filled `paper_orders`, executed `demo_orders`), which `lab/confidence.py` scores live trading from; and `bankroll`, the one cross-venue bankroll row `lab/watcher.py` rewrites each pass (Watcher).
- `049`: the event-contract walk-forward. `market_backtests` keeps one row per test window (key gains `start_at`, `score` goes, each trade's event and the window's daily returns come in), `market_category_results` follows it, and `market_readiness` holds the markets readiness gate, one row per variant (`lab/markets/families.py`).

 A schema change is a new numbered file; never edit an applied one.

## The site

`web/app.py` reads the `trader` database through a role that only has SELECT, and has no trade controls. Every page but `/healthz` needs a Forgejo sign-in: `/login` sends the browser to Forgejo's OAuth2 authorize page, `/oauth/callback` swaps the code for the Forgejo login, and only logins in `ALLOWED_USERS` get a 30-day cookie signed with `SESSION_KEY`. The site keeps no session store, so a restart signs nobody out and rotating `SESSION_KEY` signs everyone out.

Accounts shows connected balances, dry-run bots, prediction-market paper accounts and kill-switch observations. Under Research setups, choose Create setup, name it, select a strategy and venue, and enter a hypothetical USD budget and drawdown preference. An account reference is optional. Save setup keeps these choices in this browser for the signed-in login; Edit and Delete manage them. Other browsers do not share the setups. The editor stores no balances or credentials and changes no running account or bot.

The method explanation comes from [the strategy family catalog](lab/families.toml). Historical evidence uses the selected strategy and venue's 365-day summary, with its dates and bias/confidence checks. A drawdown warning compares past observations with the preference; it is no enforcement or prediction of a future loss limit. The budget does not rerun a backtest or change displayed account balances. `npm run test:web` checks setup validation and storage conflict detection without a browser; CI runs it alongside static checks.

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

A bot is breached when equity falls `[watcher] day_loss` of capital below the first reading of the UTC day, and stays breached until the next one. This is Stage 0, so a day-loss breach only raises an alert. The day opens at the first reading, so a bot unseen at 00:00 UTC opens late, and a watcher started mid-day counts from then.

The drill proves the stopping path on one dry-run bot. Run these in the watcher pod:

```
python lab/watcher.py --drill <bot>
python lab/watcher.py --drill-end <bot>
```

The first flags the bot; the next pass marks it breached and POSTs `/stopentry`, which keeps managing open trades. `stopped_at` minus `drill_at` in `watch` is the time taken, and the plan's gate is two cadences. The pass repeats the stop while the drill lasts, because a restarted bot comes back running. The second clears the breach and POSTs `/start`.

**One bankroll.** Each pass also rewrites the one `bankroll` row: every money (not dry-run) account on every venue, summed. Its cap is the `capital` of the `[live]` stage in `[[book.stages]]`, and its hard stop is `[book] hard_stop` of that capital below the peak of the summed profit (equity less capital, so an account joining or leaving moves it only by its own profit). An unseen money bot counts at its last good read. Past either, every money account gets `/stopentry` each pass, its `stopped_at` holds past the UTC day, and `lab/guard.py` refuses any live start. Open positions are not exited automatically. The breach holds until a human clears it, which starts nothing:

```
python lab/watcher.py --bankroll-clear
```

Dry-run bots and paper accounts are never in the bankroll, so today it reads 0 accounts. `/book` shows it under Budget.

The event-contract paper league's accounts (`paper-<family>-<variant>`) get watch rows too, read from `paper_accounts` rather than an API, with `seen_at` the last paper pass. A drill on one sets `stopped_at`, and the next `markets.paper` pass takes no entries for it while its open fills still settle and mark; `--drill-end` clears the row, which is all that starts it again.

### Metrics

`web/exporter.py` reads the database on each scrape, so the numbers are as fresh as the tables: bot state is as old as the newest ingest (every 5 minutes). home-config alerts on these:

| Metric | Labels | Meaning |
|---|---|---|
| `trader_db_up` | | 0 when the scrape could not read the database; every other metric is absent then |
| `trader_bot_dry_run` | `bot` | 1 dry run, 0 trading real money, per the newest snapshot |
| `trader_bot_snapshot_age_seconds`, `trader_bot_snapshot_error` | `bot` | Age of the newest snapshot, and 1 when ingest got an API error instead of numbers |
| `trader_bot_open_trades`, `trader_bot_closed_trades` | `bot` | The newest snapshot's numbers |
| `trader_bot_profit_pct` | `bot`, `cohort` | The newest snapshot's profit of all trades, percent of starting balance |
| `trader_ingest_age_seconds` | | Since any snapshot was written |
| `trader_watch_age_seconds` | | Since the watcher last finished a pass |
| `trader_watch_unseen_seconds`, `trader_watch_running`, `trader_watch_breached`, `trader_watch_day_loss_ratio` | `bot` | Since the last good read, 1 when taking entries, 1 after a daily loss breach, and the loss so far today as a share of capital |
| `trader_run_started_timestamp_seconds`, `trader_run_finished_timestamp_seconds`, `trader_run_ok`, `trader_run_last_ok_timestamp_seconds` | `kind` (`analysis`, `walkforward`, `reproduce`, `calibration`) | Newest run started, newest run finished and whether it succeeded, newest success |
| `trader_candle_age_seconds`, `trader_coverage_age_seconds`, `trader_coverage_series` | `exchange` | Newest stored candle, newest backfill write, series stored |
| `trader_futures_age_seconds` | `kind`, `exchange` | Newest funding (`funding`) or open interest (`open_interest`) row |
| `trader_suggestions` | `kind` | Open suggestions |
| `trader_markets_sweep_age_seconds`, `trader_markets_sweep_ok` | `venue` | Since the newest successful recorder sweep ended, and 1 when the newest sweep succeeded. A venue reports while it has a sweep in the last 7 days |
| `trader_markets_sweeps_1h`, `trader_markets_sweeps_failed_1h` | `venue` | Recorder sweeps started in the last hour, and those that failed (an adapter or venue error each) |
| `trader_markets_sweep_seen`, `trader_markets_sweep_changes`, `trader_markets_sweep_duration_seconds` | `venue` | The newest successful sweep's open outcomes, changed quotes written, and run time |
| `trader_markets_links` | `source` (`rule`, `llm`), `status` | Cross-venue links |
| `trader_broker_equity`, `trader_broker_read_age_seconds` | `broker` | A broker paper account's equity at its last good read, and the time since. A real account exports only its read age |
| `trader_paper_equity`, `trader_paper_capital`, `trader_paper_open_positions`, `trader_paper_marked_age_seconds` | `account` | Each marked paper account's equity, starting capital, open positions and time since its last mark |
| `trader_markout_entries`, `trader_markout_net`, `trader_markout_null` | `bot`, `horizon` | Entries marked, their mean markout (BTC beta hedged, net of the round-trip fee) and the random-entry null's, from `markouts` |
| `trader_shadow_accounts`, `trader_shadow_marked_age_seconds` | `cohort`, `state` | Shadow paper accounts per cohort and state, and seconds since the end of the newest day marked (`lab/shadow.py`) |
| `trader_markout_fill_slip` | `bot` | Mean fill over the entry candle's open, positive when worse |
| `trader_reconcile_trades` | `bot`, `kind` (`matched`, `live_only`, `bt_only`) | Paper trades against the drift backtest, from `reconcile` |
| `trader_calibration_fpr` | `gate`, `oos_months` | Share of null strategies (no edge) the gate passed over every calibration universe; gate `stack` is every research gate together |
| `trader_calibration_power` | `gate`, `edge`, `oos_months` | The same share for strategies with a planted edge of that size |
| `trader_shortfall`, `trader_shortfall_delay_seconds` | `bot`, `component` (`price`, `fee`, `profit`) | Mean paper minus backtest gap per matched trade, and the mean entry delay |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Research setup cannot be saved | Browser storage is unavailable or full | Keep the editor open to inspect the unsaved setup; allow site storage or free space before saving |
| Research setups show invalid saved data | The browser's saved setup data cannot be read or validated | Use the explicit remove-saved-data action to discard it, then create a setup again |
| Saving asks you to reload current setups | Storage could not be read initially, or another tab changed the saved setups | Keep any unsaved choices separately, then reload before saving to preserve the current saved data |
| A setup has no historical evidence or an unavailable selection | That strategy/venue has no 365-day summary yet, or a saved choice has left the catalog | Choose an available pair or keep the setup for reference; check Runs for analysis progress |
| An account observation is stale | The newest ingested snapshot is older than the site's stale interval | Check the ingest or paper job; changing a research setup does not refresh account data |
| A `watch` row has `error` set and `seen_at` falling behind | That bot's API did not answer, or an open trade has no price so `profit_all_coin` is NaN | The row clears on the next good read; TraderWatchBotBlind fires after 5 minutes |
| A master build went green but no home-config PR appeared | `FORGEJO_DEPLOY_TOKEN` is unset (the `bump-home-config` log warns), or master moved on and the newer build owns the bump | Set the secret (see CI) and re-run the job; a 401 or 403 in its log means the token expired or lost write on home-config |
| `migrate: 00N_x.sql changed after it was applied` | An applied migration file was edited | Revert the edit and put the change in the next numbered file |
| `DATABASE_URL is not set` (exit 2) | The job has no `trader-db` secret mounted | home-config `apps/freqtrade/trader-db-secret.sops.yaml` holds it |
| An ingest snapshot has `error` set and no trades moved | That bot's API was unreachable that hour | The next run catches up: trades are upserted from the bot's own history |
| `FileExistsError: [Errno 17] File exists: '/repo/user_data/hyperopts'` (or `exists already and is not a directory`) | freqtrade's userdir check-then-mkdir races when several freqtrade processes start at once (one per backfill exchange, one per analysis worker) | The image runs `freqtrade create-userdir`, freqtrade's own list (`Dockerfile`); `test_the_image_has_every_userdir_folder` fails when one is missing |
| `Historic klines not available for Kraken` | Kraken serves no OHLCV history through ccxt | Backfill collects Kraken forward (`FORWARD_EXCHANGES`); the analysis leaves it out of `ANALYZE_EXCHANGES` until months of candles exist |
| Kraken `--dl-trades` crawls (minutes per day of history) | BTC/USD prints about 300k trades a day, and freqtrade reloads and re-converts the whole trades file every run | Not used: forward OHLCV collection gets the same candles in one call per series |
| A Kraken series in `data_coverage` has `gaps` above 0 | The backfill job did not run for longer than 720 candles of that timeframe (60h at 5m) | The gap is permanent; check why the CronJob stopped |
| `backfill: <ex>: no 4h candles on this exchange, built from 1h` | coinbase offers no 4h | Expected; the 4h file is resampled from 1h. `skipped` (not `built from`) means no served timeframe divides it |
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
| Climate scorecard has mixtures but no strategies while walk-forward is running | The last scorecard preceded the first dated OOS trades | The job refreshes the scorecard after its folds finish; compare `computed_at` with fold `computed_at` before diagnosing missing data |
| A walk-forward month says `no hyperopt epoch reached N training trades` | The strategy trades too rarely on the training span for any epoch to count | Expected for slow strategies; the month keeps its as-is backtest. Lower `WF_MIN_TRADES` or raise `WF_TRAIN_DAYS` to tune it |
| `The following spaces were not found in the strategy: buy, sell` in a walk-forward log | The strategy declares no hyperopt parameters | Harmless: `--ignore-missing-spaces` tunes ROI, stoploss, trailing and max open trades anyway |
| `token is a required property` | The config schema checks the telegram block even when disabled | There is no telegram block; do not add one back empty |
| `'' is too short` or a JWT length error | `api_server` secrets are validated at load | `league.py` writes them from env at start; use `openssl rand -hex 32` for the JWT secret |
| `ModuleNotFoundError: ta` | The image has no `ta` package | GodStra, Heracles and Zeus were deleted for this |
| Futures strategies missing | US exchanges are spot only | `user_data/strategies/futures/` was deleted |
| binance.com or bybit refuse to connect (451, 403) | They geo-block US IPs, market data included | okx and kucoin serve public market data to US IPs; they are research venues, with no account |
| The analysis pod is `OOMKilled` within a minute | Too many 1m tasks at once: a year of 1m on 10 pairs peaks at 4 GB, a 5m year at 1.1 GB | The pool admits tasks by estimated memory (`ANALYZE_MEMORY_GB`); if a new venue or timeframe still overflows, re-measure and update `TASK_BASE_GB` and `GB_PER_MCANDLE` in `analyze.py` |
| The analysis pod is `OOMKilled` hours in, near the last chunk | Before 2026-11-13 the parent kept every finished chunk's trades until the loop ended; a full rerun (17k chunks, 4.9M trades, every cache key moved) passed 14Gi | Fixed: each result is freed once stored, so the parent holds only the tasks in flight |
| The analysis sits at 0 CPU with a chunk or two left until its deadline | A task estimated above `ANALYZE_MEMORY_GB` runs alone, and the float GB budget never drained to exactly 0 (2026-11-13) | Fixed: the budget counts whole MB |
| Every walk-forward fold fails with `Fatal exception! \| 1` | Hyperopt's `--random-state` must fit in 32 bits for numpy and optuna | Fixed in `metrics.seed_of`; the fold error now keeps the whole message |
| `pod ordinal N has no roster row` | StatefulSet replicas exceed roster rows | Set replicas to the roster row count in home-config |
| `overlay '<id>' is not in .../variants.toml` | A roster row names an overlay the deployed image's `lab/variants.toml` does not declare (taken out, or a newer image than the one running) | Put the overlay back in the toml, or set the row's overlay to `base`; the ids are in the `overlays` table |
