# Docs

The repo README is the reference for layout, the analysis and the site. These pages go deeper on one subject each.

## Live

- [live.md](live.md): why nothing trades money, what `lab/live.py` would check, how a human lifts the guard, reconciliation and backups.

## Markets

- [markets.md](markets.md): the venue reference behind `lab/markets/` (Kalshi, Polymarket, PredictIt, Manifold, The Odds API), the recorder, links, backtest, paper league and demo orders.

## Short a bust

- [short-bust.md](short-bust.md): the research result on shorting a bust from `lab/research/bust.py`, its limits and which route, if any, is worth taking.

## Operations

These live in the [README](../README.md):

- [Run locally](../README.md#run-locally): one bot, and migrations and tests against a throwaway Postgres.
- [CI](../README.md#ci): what `.github/workflows/image.yml` runs before an image is published.
- [Price data](../README.md#price-data) and [The database](../README.md#the-database): backfill, venues and the numbered migrations.
- [The site](../README.md#the-site), [Watcher](../README.md#watcher) and [Metrics](../README.md#metrics): the pages, the daily loss watch and the exporter.
- [Troubleshooting](../README.md#troubleshooting): symptom, cause, fix.
