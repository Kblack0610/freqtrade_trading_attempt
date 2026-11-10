# Deploy trader

This runbook describes the repo's delivery path to home-k3s. The cluster's manifests are owned by home-config at `apps/freqtrade/`; consult that checkout for current commands, schedules, probes, secret sources and storage before operating the target.

## Build and promotion

[image.yml](../../.github/workflows/image.yml) runs on PRs and master pushes. Its checks cover secrets, typecheck, lint, migrations, unit/database tests, strategy loading and the FreqAI image. Only master pushes publish to `ghcr.io/kblack0610/trader` and `ghcr.io/kblack0610/trader-ml`, with commit and latest tags. The standard image's published digest is the deployment input.

After publish, [ci/bump_home_config.py](../../ci/bump_home_config.py) updates the `trader` image entry in home-config's `apps/freqtrade/kustomization.yaml`. It opens a Forgejo PR, closes superseded bump PRs and squash-merges its own bump. Flux reconciles that merge to home-k3s. The bump skips a superseded master commit; a missing `FORGEJO_DEPLOY_TOKEN` warns and skips promotion, and a failed merge leaves a PR for review. [README CI](../../README.md#ci) owns the token setup and exact workflow commands.

Merging this repo can therefore trigger deployment through that integration. Building an image locally or opening a PR does not promote it. Changes to roster membership, schedules or workload resources belong in home-config rather than this repo.

## Configuration and verification

Runtime jobs receive `DATABASE_URL`; bot API credentials and the roster are supplied by the deployment. The web server additionally requires Forgejo OAuth and session configuration, listed by name in [the site reference](../../README.md#the-site). Keep values in the deployment's secret source. Local builds and tests are in [Run locally](../../README.md#run-locally).

Before promotion, confirm the image checks passed and the intended digest matches the build. After the home-config merge, verify Flux reconciliation, workload readiness and running image digests against that target's manifests. Check `/healthz` on the web service, bot API health and job completion. Docker's [health check](../../lab/healthcheck.py) only tests HTTP reachability; it does not establish database readiness or completed ingestion. Check the site's data freshness and the exporter/watch signals in [Metrics](../../README.md#metrics).

## Recovery and rollback

For an image rollback, use a reviewed home-config PR restoring the previously verified `trader` digest in `apps/freqtrade/kustomization.yaml`, then verify Flux and workload health again. A later master build can supersede that digest through the automatic bump, so coordinate subsequent merges with recovery. This is an operator action; this documentation change performs no rollback.

An image rollback does not undo SQL migrations, restore Postgres or reset per-bot sqlite state. Review migration compatibility before selecting an older image. The shared Postgres backup path and bot-state recovery requirements are described in [the database](../../README.md#the-database) and [live backups and recovery](../live.md). Use home-config's storage and backup runbooks for the actual target.

## Troubleshooting

| Symptom | Check and action |
|---|---|
| Image built, deployment unchanged | Check `bump-home-config` for a superseded commit, missing token or failed Forgejo merge; inspect the home-config digest and Flux reconciliation. |
| Static checks fail locally | Install `lab/requirements-dev.txt` into the active Python environment and run the matching `package.json` command from the repo root. |
| Database tests skip or fail | Use disposable Postgres and set `DATABASE_URL`; apply migrations before running tests. Never point the suite at the deployed database. |
| Pod ready, results stale | Inspect ingestion and analysis job completion and the data freshness views; HTTP health alone does not cover the research pipeline. |

The [README troubleshooting table](../../README.md#troubleshooting) owns bot, exchange and analysis failures.
