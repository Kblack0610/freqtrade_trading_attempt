# Trader TSD

Implements the [PRD](prd.md). The component map is [architecture](../../architecture.md); the module inventory is the [README](../../../README.md#layout).

## Architecture

One image, `ghcr.io/kblack0610/trader`, runs every part: bot pods, nightly and hourly jobs, and the site. Deploy manifests live in home-config `apps/freqtrade/`; a change goes live when home-config bumps the image digest (`docs/operations/deploy.md`).

- Bots: `lab/league.py` picks a roster row by pod ordinal and starts `freqtrade trade` in dry run.
- Data: `lab/backfill.py` writes candles to the lab PVC; `lab/ingest.py` reads bot APIs; `lab/markets/` records venue quotes to MinIO parquet and metadata to Postgres.
- Analysis: `lab/analyze.py` and `lab/walkforward.py` backtest and store results; `lab/metrics.py` and `lab/confidence.py` score them; `lab/suggest.py` turns scores into promote, watch or demote suggestions.
- Site: `web/app.py`, FastAPI and Jinja, reads Postgres through a SELECT-only role.

## Data model

Postgres database `trader`, schema from numbered files in `lab/migrations/` applied by `lab/migrate.py`. Tables hold trials, chunks, folds, derived metrics, paper accounts and decisions, market metadata and job runs.

## Acceptance criteria and verification

| AC | How it is satisfied | Verified by |
|---|---|---|
| AC1 | `lab/league.py` refuses keys and non-dry-run config; `lab/live.py` refuses while `GUARD_LIFTED` is false | `lab/tests/test_league.py`, `test_live.py` |
| AC2 | One paper pass and ledger; every client listed on a money map | `lab/tests/test_money.py` |
| AC3 | Registry `lab/jobs.toml` resolved by `python -m lab run <job>` | `lab/tests/test_jobs.py` |
| AC4 | Block bootstrap intervals, deflated Sharpe and gates as data in `lab/suggest.toml` | `lab/tests/`, `lab/calibration.py` |
| AC5 | `web/auth.py` gates every route; no write routes | `web/tests` |

## Risks

Overfitting is the main one; the gates and the calibration job exist to measure it. A shared node (razer-laptop) can run out of memory when analysis and walk-forward overlap.
