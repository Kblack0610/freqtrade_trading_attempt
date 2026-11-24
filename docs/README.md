# Docs

The repo README is the reference for layout, the analysis and the site. These pages go deeper on one subject each.

- Glossary: one vocabulary for the site (paper, league, account, replay account, cohort, lane, decision) and the trading terms from lot size to closing line value, in `web/glossary.toml` and rendered on `/guide#glossary`.
- [Overview](overview.md): features and implementation status.
- [Architecture](architecture.md): components, data flow and storage boundaries.
- [Standards](standards.md): contributor checks and their scope.
- [Deployment](operations/deploy.md): image promotion to home-k3s, verification and rollback.
- [Pipeline](pipeline.md): the research pipeline step by step in plain words: price data, nightly analysis, walk-forward, scoring, the paper league, where the time goes and troubleshooting.
- [Operating rules](operating-rules.md): the rules that bind the operator, paper league included, written before any real money.
- [Brand](brand.md): the site palette and why there is no logo.
- [Support](support.md): where problems land when the only user is the operator.
- [Anytime-valid gates](anytime-gates.md): proposed confidence-sequence replacements for the drift and dry run gates, option a chosen, build queued.

## Scope and legal

- [scope/trader/](scope/trader/prd.md): the PRD and TSD, with the acceptance criteria each test checks.
- [legal/](legal/PRIVACY.md): privacy and terms for a one-operator tool.

## Knowledge

- [knowledge/](knowledge/README.md): trading terms, a ranked reading list, free resources and the evidence on edge in event contracts.

## Live

- [live.md](live.md): why nothing trades money, what `lab/crypto/live.py` would check, how a human lifts the guard, reconciliation and backups.

## Platforms

- [platforms.md](platforms.md): every broker, exchange, wallet and market we use or might use, what an agent can automate on each (API, the agent machine, the person), sign-in and 2FA, terms on automation, kill switches, paper paths and cost.

## Markets

- [markets.md](markets.md): the venue reference behind `lab/events/` (Kalshi, Polymarket, PredictIt, Manifold, The Odds API), the recorder, links, backtest, paper league, the inputs registry and demo orders.

## Strategy coverage

- [Avenues register](avenues.md): considered research and trading paths, verdicts, evidence and revisit triggers.
- [Retune reviews](retune-reviews.md): one verdict per reviewed monthly retune fit, before anyone saves its params.
- [carver-afts.md](carver-afts.md): the 30 strategies and four tactics of Carver's *Advanced Futures Trading Strategies*, each mapped to spot crypto, our version, when we can use it and its avenue.

- [strategy-coverage.md](strategy-coverage.md): what a dry-run bot costs, why every strategy runs on binanceus only (the `strategies` cohort), the strategy family catalogue (scanned, staged, blocked), what diversity means (the archive axes, the cohort charter, the strategy card) and the control-set proposal.

## Short a bust

- [carry.md](carry.md): the carry research from `lab/futures/research/carry.py` (Carver AFTS 10, 11, 15, 16, 20) on recorded funding: what a spot-long, perp-short harvest earned and whether funding forecasts spot.
- [short-bust.md](short-bust.md): the research result on shorting a bust from `lab/futures/research/bust.py`, its limits and which route, if any, is worth taking.

## Operations

These live in the [README](../README.md):

- [Run locally](../README.md#run-locally): one bot, and migrations and tests against a throwaway Postgres.
- [CI](../README.md#ci): what `.github/workflows/image.yml` runs before an image is published.
- [Price data](../README.md#price-data) and [The database](../README.md#the-database): backfill, venues and the numbered migrations.
- [The site](../README.md#the-site), [Watcher](../README.md#watcher) and [Metrics](../README.md#metrics): the pages, the daily loss watch and the exporter.
- [Troubleshooting](../README.md#troubleshooting): symptom, cause, fix.
