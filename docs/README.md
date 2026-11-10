# Docs

The repo README is the reference for layout, the analysis and the site. These pages go deeper on one subject each.

- [Overview](overview.md): features and implementation status.
- [Architecture](architecture.md): components, data flow and storage boundaries.
- [Standards](standards.md): contributor checks and their scope.
- [Deployment](operations/deploy.md): image promotion to home-k3s, verification and rollback.

## Live

- [live.md](live.md): why nothing trades money, what `lab/live.py` would check, how a human lifts the guard, reconciliation and backups.

## Platforms

- [platforms.md](platforms.md): every broker, exchange, wallet and market we use or might use, what an agent can automate on each (API, the agent machine, the person), sign-in and 2FA, terms on automation, kill switches, paper paths and cost.

## Markets

- [markets.md](markets.md): the venue reference behind `lab/markets/` (Kalshi, Polymarket, PredictIt, Manifold, The Odds API), the recorder, links, backtest, paper league and demo orders.

## Strategy coverage

- [strategy-coverage.md](strategy-coverage.md): what a dry-run bot costs, why every strategy runs on binanceus only (the `strategies` cohort), the strategy family catalogue (scanned, staged, blocked) and the control-set proposal.

## Short a bust

- [short-bust.md](short-bust.md): the research result on shorting a bust from `lab/research/bust.py`, its limits and which route, if any, is worth taking.

## Operations

These live in the [README](../README.md):

- [Run locally](../README.md#run-locally): one bot, and migrations and tests against a throwaway Postgres.
- [CI](../README.md#ci): what `.github/workflows/image.yml` runs before an image is published.
- [Price data](../README.md#price-data) and [The database](../README.md#the-database): backfill, venues and the numbered migrations.
- [The site](../README.md#the-site), [Watcher](../README.md#watcher) and [Metrics](../README.md#metrics): the pages, the daily loss watch and the exporter.
- [Troubleshooting](../README.md#troubleshooting): symptom, cause, fix.
