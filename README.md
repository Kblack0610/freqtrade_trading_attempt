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
| `lab/metrics.py` | Pure functions behind the derived tables: daily returns, Sharpe, Sortino, drawdown, block-bootstrap intervals, deflated Sharpe, Monte Carlo, BTC regimes |
| `lab/ingest.py` | Hourly job: every bot's trades (closed and open) and a snapshot (profit, balance, dry-run flag) into Postgres |
| `lab/migrate.py`, `lab/migrations/` | Schema: numbered SQL files applied once each, in order, under an advisory lock; runs as the init container of every job |
| `lab/db.py` | The one Postgres connection: `DATABASE_URL` |
| `web/` | The site: FastAPI and Jinja, read-only, Forgejo sign-in (`web/auth.py`), server-drawn SVG charts (`web/charts.py`); pages Overview, bot detail, Strategies, strategy detail, Data, Runs (see The site) |
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

Backfill knobs (env): `BACKFILL_EXCHANGES` (`binanceus coinbase kraken`), `TIMEFRAMES` (`5m 15m 30m 1h 4h 1d`, each capped at what the exchange offers), `FORWARD_EXCHANGES` (`kraken`), `PREPEND_FROM` (`20150101`), `LAB_RESULTS` (`/lab`). For a quick local run, add `-e TIMEFRAMES='4h 1d' -v "$HOME/.cache/trader-lab:/lab"` to the test container and run `python lab/backfill.py`.

## The analysis

`lab/analyze.py` runs nightly and is incremental:

- **Chunks.** Each strategy x exchange is backtested per calendar year, from the first year with candles to today, each year from the same 1000 USD wallet. A chunk is keyed by the strategy file's hash, both config files, the freqtrade version and its dates, so a past year runs once and is cached; a normal night redoes only the current year. Changing a strategy or a config reruns all of its years. A failed chunk is retried on the next two nights, then left failed until its key changes. A run is marked failed when more than 10% of its chunks fail.
- **Trades.** Every backtest trade is stored (`backtest_trades`), so every metric is recomputed from trades, never copied from freqtrade's summary.
- **Bias.** `lookahead-analysis` on `LOOKAHEAD_DAYS` of binanceus and `recursive-analysis` on `RECURSIVE_DAYS`, rerun when the file changes. Lookahead bias means the backtest saw the future: the strategy ranks last and is never suggested. Recursive bias means an indicator still moves at the strategy's own startup candle count; it is shown as a warning, because the indicator may not feed a signal. A check freqtrade refuses (for example a startup candle count of 0) is stored as "not checked" with freqtrade's reason and retried weekly.
- **Windows.** The chunks are chained into one daily return series per strategy x exchange, then measured over the last 30, 90, 180 and 365 days and the whole history: return, CAGR, Sharpe with a 90% block-bootstrap interval, deflated Sharpe, Sortino, Calmar, max drawdown, profit factor, expectancy with its interval, win rate, exposure and the share of months up.
- **Deflated Sharpe** is the probability that a Sharpe beats the best one luck alone would give across every strategy tried in that window. With about 60 strategies on two exchanges, a raw Sharpe of 1 is not evidence; a DSR near 1 is.
- **Estimates.** A seeded block bootstrap (10-day blocks, 5000 paths) of the last 365 days gives 30 and 90-day return bands (p10, p50, p90), the chance of a loss and the expected worst drawdown. Same data, same bands. It needs at least 180 days.
- **Regimes.** Every day gets a BTC trend (above or below its 200-day mean) and a volatility third (30-day realized volatility against its own history), and the strategy page splits returns by both.
- **Correlations.** Each strategy's daily returns against every strategy the league runs on that exchange, over the last 365 days; near 1 means it would only duplicate a bot already running.

The leaderboard ranks by the low end of the Sharpe interval, so a strategy has to look good on its unlucky draw. Rows under 20 trades sink, and lookahead-biased rows go last. Promoting a strategy is still a human edit to the roster in home-config.

## Adding a strategy

Drop the `.py` in `user_data/strategies/` (any subfolder). It must import on the pinned image: Python 3.14, NumPy 2 (`np.nan`, not `np.NaN`), no `ta` package. Check with `freqtrade list-strategies --recursive-strategy-search --userdir user_data -c user_data/config.base.json -c user_data/exchanges/binanceus.json`. The next nightly analysis backtests its whole history and checks it for bias.

## Price data

`lab/backfill.py` owns the candles; nothing else downloads them. Per exchange and timeframe:

- A series that has not reached the exchange's oldest candle gets a `download-data --prepend --timerange 20150101-` pass. One call reaches the listing date (binanceus from 2019, coinbase from 2015 for BTC).
- Then every series gets an append pass up to now. A prepend drops a series' newest candle, so the append always runs after it.
- When a prepend leaves the first candle where it was, the series is marked `oldest_reached` and only appended from then on.
- Kraken serves no OHLCV history, only its newest 720 candles per timeframe. It is collected forward through ccxt and merged into the same Freqtrade files. A run at least every 60 hours keeps 5m gap-free; a longer outage leaves a gap that `data_coverage` counts. These candles exist nowhere else once they age out.

The first run fetches full history (5m since 2019 on binanceus is the long pole) and can take hours; later runs take under a minute. Exchanges run in parallel, each with its own rate limit.

## The database

- `001`: `bots`, `bot_trades` (one row per trade, keyed by bot, trade id and open date, so a bot whose sqlite is lost keeps its old trades), `bot_snapshots` (hourly from ingest; rows with `source = 'report'` came from the retired daily report), `runs`.
- `002`: `data_coverage`, first and last candle, rows, gaps and missing candles per exchange x pair x timeframe, written by every backfill run.
- `003`: the analysis. `strategies` (every file found, its hash and load error), `backtest_chunks` and `backtest_trades` (the cache and its trades), `bias_checks`; derived, rebuilt in one transaction at the end of each run: `strategy_daily`, `strategy_windows`, `estimates`, `regimes`, `correlations`. `runs` rows of kind `analysis` carry the run summary in `raw`.

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

The site refuses to start when a sign-in variable is missing. Locally, against a copy of the data:

```sh
docker run --rm -p 127.0.0.1:18765:8000 --entrypoint uvicorn -e DATABASE_URL=... -e FORGEJO_URL=https://git.example \
  -e OAUTH_CLIENT_ID=x -e OAUTH_CLIENT_SECRET=x -e SESSION_KEY=$(openssl rand -hex 32) trader web.app:app --host 0.0.0.0 --port 8000
```

Sign-in cannot complete against a fake Forgejo; mint a cookie with `auth.make_session(auth.Config(), "kblack0610")` under the same `SESSION_KEY` and send it as `trader_session`.

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
| A run's Skipped list says `<strategy> on <ex>: no 1m candles` | The strategy's timeframe is not backfilled there (BinHV45 wants 1m) | Expected until 1m is added to backfill's `TIMEFRAMES` |
| A strategy shows `fails to load` | It does not import on the pinned image | Fix it or delete it; CI fails on a load error, so this only shows for a file that broke after a freqtrade bump |
| Recursive check `not checked: invalid startup candle count of 0` | The strategy sets no `startup_candle_count` | Set one in the strategy; the lookahead verdict still counts |
| `pandas` `get_loc() got an unexpected keyword argument 'method'` or `fillna(method=...)` | pandas 3 removed both | Use `get_indexer(..., method=...)` and `ffill()`; FixedRiskRewardLoss was deleted for the first |
| coinbase backfill logs hundreds of `429 Too Many Requests` on its first run | The first deep 5m prepend pulls ten years of candles and outruns coinbase's public rate limit | freqtrade retries; the run that fails is finished by the next hour's run, and later runs append about 300 candles each |
| The site answers `Sign-in expired or was not started here` (400) | The callback arrived without the state cookie `/login` set, or more than 10 minutes later | Open `/login` again; a link to `/oauth/callback` from history cannot sign in |
| The site exits with `trader-web: missing ...` | A sign-in variable is not set | home-config `apps/freqtrade/trader-web-oauth-secret.sops.yaml` holds them |
| `token is a required property` | The config schema checks the telegram block even when disabled | There is no telegram block; do not add one back empty |
| `'' is too short` or a JWT length error | `api_server` secrets are validated at load | `league.py` writes them from env at start; use `openssl rand -hex 32` for the JWT secret |
| `ModuleNotFoundError: ta` | The image has no `ta` package | GodStra, Heracles and Zeus were deleted for this |
| Futures strategies missing | US exchanges are spot only | `user_data/strategies/futures/` was deleted |
| binance.com, bybit or okx refuse to connect | They geo-block US IPs | Use binanceus, coinbase, kraken |
| `pod ordinal N has no roster row` | StatefulSet replicas exceed roster rows | Set replicas to the roster row count in home-config |
