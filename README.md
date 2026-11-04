# freqtrade_trading_attempt

Strategy code and tooling for a Freqtrade dry-run league: a set of paper-trading bots, each running one strategy on one exchange, plus weekly backtests that show which strategies are worth watching. No real money, ever.

The league runs on home-k3s. Its deploy (roster, StatefulSet, CronJobs, secrets) lives in home-config at `apps/freqtrade/`. Every pod runs one image, `ghcr.io/kblack0610/trader`: the pinned `freqtradeorg/freqtrade:2026.9` release plus psycopg plus this repo at `/repo`. GitHub Actions builds it on each push to master (tags `sha-<7>` and `latest`), and home-config pins it by digest, so a change here goes live only when home-config bumps the digest.

Results go to the `trader` database in the cluster's shared Postgres, which the nightly `pg_dumpall` backs up.

## Layout

| Path | What it is |
|---|---|
| `user_data/strategies/` | Strategies, found with `--recursive-strategy-search` |
| `user_data/config.base.json` | Shared config: dry run, 1000 USD wallet, spot, static pair list |
| `user_data/exchanges/<name>.json` | Per-exchange overlay: name, pair whitelist, pricing quirks; no keys |
| `lab/league.py` | Bot entrypoint: picks its roster row from the pod ordinal, refuses anything but key-less spot dry run, writes the API config, execs `freqtrade trade` |
| `lab/experiments.sh` | Weekly job: download data, backtest every strategy per exchange, lookahead check, hyperopt the best few |
| `lab/summarize.py` | Turns one experiments run into `summary.json` and `summary.md` |
| `lab/report.py` | Daily job: reads every bot's REST API, appends `league-history.jsonl`, writes `reports/<date>.md` with the leaderboard and promote candidates |
| `lab/ingest.py` | Hourly job: every bot's trades (closed and open) and a snapshot (profit, balance, dry-run flag) into Postgres, plus an idempotent import of `runs/*/summary.json` and `league-history.jsonl` from the lab PVC |
| `lab/migrate.py`, `lab/migrations/` | Schema: numbered SQL files applied once each, in order, under an advisory lock; runs as the init container of every job |
| `lab/db.py` | The one Postgres connection: `DATABASE_URL` |
| `lab/tests/` | pytest; the DB tests need `DATABASE_URL` pointing at a throwaway database |
| `Dockerfile`, `lab/requirements.txt` | The `trader` image |
| `.github/workflows/image.yml` | CI: build, migrate twice, tests, every strategy loads; on master also push to GHCR |

## The no-real-money guard

`lab/league.py` exits 2 before freqtrade starts if any `FREQTRADE__` env var is set, if the merged config is not `dry_run: true`, if it is not spot, or if the exchange block carries a key, secret, password, uid, private key or wallet address. Public market data needs no keys, so the exchange files keep them empty. `lab/report.py` and `lab/ingest.py` exit 1 if any bot's `show_config` says it is not dry run.

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

A quick experiments run (2 strategies, 14 days, two timeframes):

```sh
mkdir -p /tmp/lab
docker run --rm -v "$PWD:/repo" -v /tmp/lab:/lab \
  -e EXPERIMENT_STRATEGIES="SampleStrategy Bandtastic" -e BACKTEST_DAYS=14 -e LOOKAHEAD_DAYS=7 \
  -e TIMEFRAMES="5m 1h" -e HYPEROPT_EPOCHS=5 \
  --entrypoint bash trader /repo/lab/experiments.sh
cat /tmp/lab/runs/latest/summary.md
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

Experiment knobs (env): `EXPERIMENT_EXCHANGES` (default `binanceus coinbase`), `EXPERIMENT_STRATEGIES`, `BACKTEST_DAYS` (90), `LOOKAHEAD_DAYS` (30), `TIMEFRAMES` (`5m 15m 30m 1h 4h 1d`), `HYPEROPT_TOP` (3), `HYPEROPT_EPOCHS` (100), `HYPEROPT_JOBS` (4).

## Reading the results

- `runs/<date>/summary.md`: every strategy x exchange backtest ranked by profit, with its lookahead verdict (BIASED, clean, unchecked). Rows under 20 trades sink to the bottom; they are noise.
- Hyperopt runs only for strategies that are profitable, have at least 20 trades and checked clean of lookahead bias. Parameters are not exported back into the strategy; they sit in the summary for a human to adopt.
- `reports/<date>.md`: the live league ranked by closed-trade profit, plus promote candidates from the latest experiments. Promoting one is a human edit to the roster in home-config.
- `league-history.jsonl`: one line per bot per day. A strategy that only looks good for one week is luck; this file is how to tell.

## Adding a strategy

Drop the `.py` in `user_data/strategies/` (any subfolder). It must import on the pinned image: Python 3.14, NumPy 2 (`np.nan`, not `np.NaN`), no `ta` package. Check with `freqtrade list-strategies --recursive-strategy-search --userdir user_data -c user_data/config.base.json -c user_data/exchanges/binanceus.json`. The next experiments run picks it up.

## The database

Tables (`lab/migrations/001_init.sql`): `bots`, `bot_trades` (one row per trade, keyed by bot, trade id and open date, so a bot whose sqlite is lost keeps its old trades), `bot_snapshots` (hourly from ingest, daily rows imported from `league-history.jsonl` have `source = 'report'`), `runs`, `backtests`, `bias_checks`, `hyperopt_runs`. A schema change is a new numbered file; never edit an applied one.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `migrate: 00N_x.sql changed after it was applied` | An applied migration file was edited | Revert the edit and put the change in the next numbered file |
| `DATABASE_URL is not set` (exit 2) | The job has no `trader-db` secret mounted | home-config `apps/freqtrade/trader-db-secret.sops.yaml` holds it |
| An ingest snapshot has `error` set and no trades moved | That bot's API was unreachable that hour | The next run catches up: trades are upserted from the bot's own history |
| `Historic klines not available for Kraken` | Kraken serves no OHLCV history through ccxt | Kraken runs live dry-run bots only; it is left out of `EXPERIMENT_EXCHANGES` |
| `Orderbook not available for Coinbase Advanced` | freqtrade has no order book for coinbase | `exchanges/coinbase.json` sets `use_order_book: false` for entry and exit pricing |
| `Invalid timeframe '4h'` on coinbase | coinbase offers 1m 5m 15m 30m 1h 2h 6h 1d only | Data downloads one timeframe per call, so only 4h strategies fail on coinbase |
| A backtest dir has `error.txt` and no zip | freqtrade exits 0 on some config errors | The zip is the proof of a run; read `run.log` in the same dir |
| `No data found` for a strategy | Its timeframe is not in `TIMEFRAMES` (BinHV45 wants 1m) | Add the timeframe, or accept the failed row |
| `token is a required property` | The config schema checks the telegram block even when disabled | There is no telegram block; do not add one back empty |
| `'' is too short` or a JWT length error | `api_server` secrets are validated at load | `league.py` writes them from env at start; use `openssl rand -hex 32` for the JWT secret |
| `ModuleNotFoundError: ta` | The image has no `ta` package | GodStra, Heracles and Zeus were deleted for this |
| Futures strategies missing | US exchanges are spot only | `user_data/strategies/futures/` was deleted |
| binance.com, bybit or okx refuse to connect | They geo-block US IPs | Use binanceus, coinbase, kraken |
| `pod ordinal N has no roster row` | StatefulSet replicas exceed roster rows | Set replicas to the roster row count in home-config |
