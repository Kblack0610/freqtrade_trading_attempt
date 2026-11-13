# Trader overview

Trader is a research and paper-trading application built around Freqtrade. This reference describes its features and implementation status; the [architecture](architecture.md) maps the components.

## Features

- Runs spot strategies in dry-run cohorts from a roster, with exchange and parameter overlays (`lab/league.py`, `lab/cohorts.toml`, `lab/variants.toml`).
- Collects candles and bot results, runs backtests and walk-forward folds, and scores confidence, decay, portfolios and mixtures (`lab/backfill.py`, `lab/ingest.py`, `lab/analyze.py`, `lab/walkforward.py`).
- Records event-contract markets and evaluates paper accounts across venues (`lab/markets/`, [venue reference](markets.md)).
- Reads wallet and broker balances through the paths in `lab/wallet.py` and `lab/broker.py`; the Alpaca client in `lab/broker.py` restricts orders to the paper endpoint.
- Shows research results, cohorts, coverage and markets through the Forgejo-authenticated site (`web/app.py`), with Prometheus exporters and a loss watcher.

## Status

The implemented trading league is dry run. `lab/league.py` rejects exchange credentials, environment overrides and configurations outside spot dry run. The live entry point in `lab/live.py` refuses while `GUARD_LIFTED` in `lab/guard.py` is false; [live operations](live.md) owns the requirements for changing that boundary. Paper-market and broker paths have separate controls described in [platforms](platforms.md).

The repo contains an image build, database migrations, tests and a digest-bump integration for home-k3s. Deployment manifests and secrets belong to home-config, so source and a successful image build alone do not establish the live cluster's health. This page records implementation status; it makes no profitability or current deployment-health claim.

Start with the [README](../README.md#run-locally) for build, run and test commands, [standards](standards.md) for contributor checks, and [deployment](operations/deploy.md) for the delivery path.
