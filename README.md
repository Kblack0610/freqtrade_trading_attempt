# freqtrade_trading_attempt

Strategy code and tooling for a Freqtrade dry-run league: a set of paper-trading bots, each running one strategy on one exchange, plus a nightly analysis that backtests every strategy over all the price history there is and ranks them with confidence intervals, bias checks and return estimates. No real money, ever.

The league runs on home-k3s. Its deploy (roster, StatefulSet, CronJobs, secrets) lives in home-config at `apps/freqtrade/`. Every pod runs one image, `ghcr.io/kblack0610/trader`: the pinned `freqtradeorg/freqtrade:2026.9` release plus psycopg plus this repo at `/repo`. GitHub Actions builds it on each push to master (tags `sha-<7>` and `latest`), and home-config pins it by digest, so a change here goes live only when home-config bumps the digest.

Results go to the `trader` database in the cluster's shared Postgres, which the nightly `pg_dumpall` backs up. The site in `web/` shows them, behind a Forgejo sign-in; home-config deploys it.

## Layout

| Path | What it is |
|---|---|
| `user_data/strategies/` | Strategies, found with `--recursive-strategy-search` |
| `user_data/config.base.json` | Shared config: dry run, 1000 USD wallet, spot, static pair list |
| `user_data/exchanges/<name>.json` | Per-exchange overlay: name, pair whitelist, pricing quirks; no keys |
| `lab/league.py` | Bot entrypoint: picks its roster row from the pod ordinal, refuses anything but key-less spot dry run, writes the API config, execs `freqtrade trade` |
| `lab/backfill.py` | Hourly job: price history for every exchange x pair x timeframe into `data/<exchange>/` on the lab PVC, and what it holds into `data_coverage` (see Price data) |
| `lab/analyze.py` | Nightly job: every strategy x exchange backtested in calendar-year chunks over the whole history, lookahead and recursive bias checks, then the derived tables (see The analysis) |
| `lab/walkforward.py` | Weekly job: hyperopt on the months before each of the last whole months, then that unseen month backtested as is and tuned (see Walk-forward) |
| `lab/metrics.py` | Pure functions behind the derived tables: daily returns, Sharpe, Sortino, drawdown, block-bootstrap intervals, deflated Sharpe, Monte Carlo, BTC regimes |
| `lab/suggest.py`, `lab/suggest.toml` | After each analysis and walk-forward: promote, watch, demote, retire, fix and retune suggestions from the derived tables, with the thresholds as data (see Suggestions) |
| `lab/ingest.py` | Hourly job: every bot's trades (closed and open) and a snapshot (profit, balance, dry-run flag) into Postgres |
| `lab/migrate.py`, `lab/migrations/` | Schema: numbered SQL files applied once each, in order, under an advisory lock; runs as the init container of every job |
| `lab/db.py` | The one Postgres connection: `DATABASE_URL` |
| `web/` | The site: FastAPI and Jinja, read-only, Forgejo sign-in (`web/auth.py`), server-drawn SVG charts (`web/charts.py`); pages Overview, bot detail, Strategies, strategy detail, Walk-forward, Suggestions, Data, Runs (see The site) |
| `lab/tests/` | pytest; the DB tests need `DATABASE_URL` pointing at a throwaway database |
| `Dockerfile`, `lab/requirements.txt` | The `trader` image |
| `.github/workflows/image.yml` | CI: build, migrate twice, tests, every strategy loads; on master also push to GHCR |

## The no-real-money guard

`lab/league.py` exits 2 before freqtrade starts if any `FREQTRADE__` env var is set, if the merged config is not `dry_run: true`, if it is not spot, or if the exchange block carries a key, secret, password, uid, private key or wallet address. Public market data needs no keys, so the exchange files keep them empty. `lab/ingest.py` exits 1 if any bot's `show_config` says it is not dry run.

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

A quick analysis of two strategies: run a backfill into the mounted `/lab` first (below), then `python lab/migrate.py && python lab/analyze.py` in the test container with `-e LAB_RESULTS=/lab -e ANALYZE_EXCHANGES=binanceus -e REGIME_EXCHANGE=binanceus -e "ANALYZE_STRATEGIES=Strategy001 SampleStrategy"`. It reads which candles exist from `data_coverage`, so the backfill has to write there too. Two strategies over about a year of binanceus data took under a minute.

Analysis knobs (env): `ANALYZE_EXCHANGES` (`binanceus coinbase`), `ANALYZE_STRATEGIES` (empty means all), `ANALYZE_WORKERS` (6 backtests at once), `BIAS_EXCHANGE` (`binanceus`), `LOOKAHEAD_DAYS` (120), `RECURSIVE_DAYS` (30), `REGIME_EXCHANGE` (`coinbase`, the longest BTC history), `CHUNK_TIMEOUT` (3600 s).

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
- **Deflated Sharpe** is the probability that a Sharpe beats the best one luck alone would give across every strategy tried in that window: the expected best of that many strategies with a true Sharpe of zero, whose estimates vary only by sampling noise. It does not use the spread of the strategies' own Sharpes, which is mostly fee drag and would set the bar near an annualized 10. With 89 strategy x exchange series over a year, the bar is about an annualized 2.5, so a raw Sharpe of 1 is not evidence; a DSR near 1 is.
- **Estimates.** A seeded block bootstrap (10-day blocks, 5000 paths) of the last 365 days gives 30 and 90-day return bands (p10, p50, p90), the chance of a loss and the expected worst drawdown. Same data, same bands. It needs at least 180 days.
- **Regimes.** Every day gets a BTC trend (above or below its 200-day mean) and a volatility third (30-day realized volatility against its own history), and the strategy page splits returns by both.
- **Drift.** Each league bot's strategy is backtested on its own exchange from the bot's start to now, with the bot's config, and set against its dry-run trades (`drift`). An entry matches when the backtest entered the same pair within one candle; the gap is live minus backtest profit per trade over trades closed on both sides. A low match rate says the backtest does not model what the bot does (startup candles, data gaps, a different pairlist); a steady negative gap is fill or fee error the backtests leave out. It needs weeks of trades to mean anything. The Overview shows the match per bot and each bot page has the comparison.
- **Correlations.** Each strategy's daily returns against every strategy the league runs on that exchange, over the last 365 days; near 1 means it would only duplicate a bot already running.

The leaderboard ranks by the low end of the Sharpe interval, so a strategy has to look good on its unlucky draw. Rows under 20 trades sink, and lookahead-biased rows go last. Promoting a strategy is still a human edit to the roster in home-config.

## Walk-forward

A backtest of a strategy as it is says nothing about tuning it, and hyperopt's best result on a span is fitted to that span. `lab/walkforward.py` runs weekly to measure how much of hyperopt's gain survives on data it never saw:

- **Candidates.** The league's strategies, then the promote suggestions, then the watch ones by score, loadable and lookahead-clean, up to `WF_LIMIT`, on `WF_EXCHANGE` only (binanceus: coinbase's 2.4% round trip leaves nothing to tune).
- **Folds.** For each of the last `WF_FOLDS` whole calendar months, hyperopt tunes the strategy on the `WF_TRAIN_DAYS` before it (`--spaces default`: ROI, stoploss, trailing, max open trades and any declared buy and sell parameters) with a random state seeded by strategy and month, so a rerun gives the same answer. Then the month is backtested as is and with the tuned parameters, and the training span is backtested with them too (in sample). Every per-trade profit ratio is kept.
- **Isolation.** Hyperopt works on a copy of the strategy's folder in a temporary userdir, because it writes its params file next to the strategy, where every nightly backtest would load it.
- **Cache.** A fold is keyed by the strategy file, both configs, the freqtrade version, its dates and the hyperopt settings, so a normal week runs only the newest month, and a changed file reruns all of that strategy's months. Failures retry like analysis chunks; a month with no candles is stored once. A fold where no epoch reached `WF_MIN_TRADES` training trades is kept, untuned, with that as its note.
- **Reading it.** The Walk-forward page pools each strategy's months: how many the tuned parameters beat, the per-trade profit as is and tuned with 90% bootstrap intervals, and walk-forward efficiency, the tuned return per day out of sample over the same in sample. Near 1 the tuning carries over; near 0 or below, hyperopt fitted noise. The strategy page has the months one by one.

| Env | Default | What it is |
|---|---|---|
| `WF_EXCHANGE` | `binanceus` | The one exchange folds run on |
| `WF_FOLDS` | `6` | Test months, the newest whole ones |
| `WF_TRAIN_DAYS` | `180` | Training span before each test month |
| `WF_EPOCHS` | `150` | Hyperopt epochs per fold |
| `WF_LOSS` | `SharpeHyperOptLossDaily` | Hyperopt loss |
| `WF_MIN_TRADES` | `10` | Training trades an epoch needs to count |
| `WF_LIMIT` | `16` | Candidates per run |
| `WF_WORKERS` | `6` | Folds at once; each hyperopt runs on one core (`-j 1`) |
| `WF_TIMEOUT` | `10800` | Seconds before one hyperopt is killed |
| `WF_STRATEGIES` | all | Space-separated names to limit a run to |

## Suggestions

`lab/suggest.py` runs at the end of every analysis (alone: `python lab/suggest.py`) and rebuilds `suggestions` from the derived tables. Every threshold is in `lab/suggest.toml`; each suggestion stores every gate it was held to with the value seen, so the site shows why.

- **Promote**: a strategy and exchange the league does not run yet that is lookahead-clean, has at least 30 trades in the last 365 days, has the low ends of its expectancy and Sharpe intervals above zero, a deflated Sharpe of at least 0.90, at least 70% of at least two full calendar years profitable, a drawdown under 35%, and daily returns that do not track a league strategy (|rho| at most 0.70). The action is the exact `roster.tsv` line and the new replica count.
- **Watch**: lookahead-clean candidates one gate short, best Sharpe interval first, so a near miss is visible before it qualifies.
- **Demote**: a league bot whose own dry run lost money over 60 days with at least 20 trades, whose strategy's 365-day backtests on that exchange lose even at the high end of the expectancy interval (kraken has no history, so for its bots every research exchange must agree), or whose strategy has lookahead bias. Rows map to pod ordinals and each pod keeps its own sqlite, so the action says to replace the row in place and clear that pod's PVC, never to delete the row.
- **Retire**: lookahead bias or a load error. It supersedes a fix for the same file.
- **Fix**: the recursive check found an indicator still moving at the startup candle count, or could not run because the strategy declares none.
- **Retune**: a lookahead-clean strategy whose tuned parameters beat it as it is on each of the newest two walk-forward months (at least 5 tuned trades each), and, pooled over every month, has at least 30 tuned trades, a higher per-trade profit than as is, and the low end of its 90% interval above zero. The action is the newest fold's params file, to save next to the strategy.

A suggestion keeps its `first_seen` while it holds, so "since Nov 05" on the site is how long the evidence has pointed the same way; one that stops applying is deleted.

## Adding a strategy

Drop the `.py` in `user_data/strategies/` (any subfolder). It must import on the pinned image: Python 3.14, NumPy 2 (`np.nan`, not `np.NaN`), no `ta` package. Check with `freqtrade list-strategies --recursive-strategy-search --userdir user_data -c user_data/config.base.json -c user_data/exchanges/binanceus.json`. The next nightly analysis backtests its whole history and checks it for bias.

## Price data

`lab/backfill.py` owns the candles; nothing else downloads them. Per exchange and timeframe:

- A series that has not reached the exchange's oldest candle gets a `download-data --prepend --timerange 20150101-` pass. One call reaches the listing date (binanceus from 2019, coinbase from 2015 for BTC). 1m stops at its `PREPEND_FLOORS` date, 2024-01-01: the 1m files are about 30 times the 30m ones, and only a few scalping strategies (BinHV45, CCIStrategy, Low_BB, ReinforcedSmoothScalp, Scalp, SmoothScalp) trade on 1m.
- Then every series gets an append pass up to now. A prepend drops a series' newest candle, so the append always runs after it.
- When a prepend leaves the first candle where it was, the series is marked `oldest_reached` and only appended from then on.
- Kraken serves no OHLCV history, only its newest 720 candles per timeframe. It is collected forward through ccxt and merged into the same Freqtrade files. A run at least every 60 hours keeps 5m gap-free; a longer outage leaves a gap that `data_coverage` counts. These candles exist nowhere else once they age out.

The first run fetches full history (5m since 2019 on binanceus is the long pole) and can take hours; later runs take under a minute. Exchanges run in parallel, each with its own rate limit.

## The database

- `001`: `bots`, `bot_trades` (one row per trade, keyed by bot, trade id and open date, so a bot whose sqlite is lost keeps its old trades), `bot_snapshots` (hourly from ingest; rows with `source = 'report'` came from the retired daily report), `runs`.
- `002`: `data_coverage`, first and last candle, rows, gaps and missing candles per exchange x pair x timeframe, written by every backfill run.
- `003`: the analysis. `strategies` (every file found, its hash and load error), `backtest_chunks` and `backtest_trades` (the cache and its trades), `bias_checks`; derived, rebuilt in one transaction at the end of each run: `strategy_daily`, `strategy_windows`, `estimates`, `regimes`, `correlations`. `runs` rows of kind `analysis` carry the run summary in `raw`.
- `004`: clears bias checks that errored before subfolder strategies could load, so they rerun.
- `005`: `suggestions`, one row per kind, strategy, exchange and bot, with its gates as JSON and when it was first and last seen.

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

### Metrics

`web/exporter.py` reads the database on each scrape, so the numbers are as fresh as the tables: bot state is as old as the newest hourly ingest. home-config alerts on these:

| Metric | Labels | Meaning |
|---|---|---|
| `trader_db_up` | | 0 when the scrape could not read the database; every other metric is absent then |
| `trader_bot_dry_run` | `bot` | 1 dry run, 0 trading real money, per the newest snapshot |
| `trader_bot_snapshot_age_seconds`, `trader_bot_snapshot_error` | `bot` | Age of the newest snapshot, and 1 when ingest got an API error instead of numbers |
| `trader_bot_open_trades`, `trader_bot_closed_trades`, `trader_bot_profit_pct` | `bot` | The newest snapshot's numbers |
| `trader_ingest_age_seconds` | | Since any snapshot was written |
| `trader_run_started_timestamp_seconds`, `trader_run_finished_timestamp_seconds`, `trader_run_ok`, `trader_run_last_ok_timestamp_seconds` | `kind` (`analysis`, `walkforward`) | Newest run started, newest run finished and whether it succeeded, newest success |
| `trader_candle_age_seconds`, `trader_coverage_age_seconds`, `trader_coverage_series` | `exchange` | Newest stored candle, newest backfill write, series stored |
| `trader_suggestions` | `kind` | Open suggestions |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
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
| coinbase backfill logs hundreds of `429 Too Many Requests` on its first run | The first deep 5m prepend pulls ten years of candles and outruns coinbase's public rate limit | freqtrade retries; the run that fails is finished by the next hour's run, and later runs append about 300 candles each |
| The site answers `Sign-in expired or was not started here` (400) | The callback arrived without the state cookie `/login` set, or more than 10 minutes later | Open `/login` again; a link to `/oauth/callback` from history cannot sign in |
| The site exits with `trader-web: missing ...` | A sign-in variable is not set | home-config `apps/freqtrade/trader-web-oauth-secret.sops.yaml` holds them |
| A walk-forward month says `no hyperopt epoch reached N training trades` | The strategy trades too rarely on the training span for any epoch to count | Expected for slow strategies; the month keeps its as-is backtest. Lower `WF_MIN_TRADES` or raise `WF_TRAIN_DAYS` to tune it |
| `The following spaces were not found in the strategy: buy, sell` in a walk-forward log | The strategy declares no hyperopt parameters | Harmless: `--ignore-missing-spaces` tunes ROI, stoploss, trailing and max open trades anyway |
| `token is a required property` | The config schema checks the telegram block even when disabled | There is no telegram block; do not add one back empty |
| `'' is too short` or a JWT length error | `api_server` secrets are validated at load | `league.py` writes them from env at start; use `openssl rand -hex 32` for the JWT secret |
| `ModuleNotFoundError: ta` | The image has no `ta` package | GodStra, Heracles and Zeus were deleted for this |
| Futures strategies missing | US exchanges are spot only | `user_data/strategies/futures/` was deleted |
| binance.com, bybit or okx refuse to connect | They geo-block US IPs | Use binanceus, coinbase, kraken |
| `pod ordinal N has no roster row` | StatefulSet replicas exceed roster rows | Set replicas to the roster row count in home-config |
