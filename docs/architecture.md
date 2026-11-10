# Trader architecture

This reference maps the code and data boundaries. The [README layout](../README.md#layout) is the module inventory; [overview](overview.md) describes the product.

## Runtime and data flow

The [Dockerfile](../Dockerfile) layers this repo and `lab/requirements.txt` onto a digest-pinned Freqtrade image at `/repo`. It retains Freqtrade's entry point; callers override it for Python jobs or the web server. CI also builds a FreqAI variant for ML research.

1. `lab/league.py` selects a roster row by ordinal, combines base, exchange and overlay configuration, applies its dry-run guard and starts Freqtrade. Strategies live in `user_data/strategies/` and staged families in `lab/families/`. Bot state and candle files are runtime volume data.
2. `lab/ingest.py` reads bot APIs into Postgres. `lab/backfill.py` collects candles and records coverage. Analysis and walk-forward jobs read those candles, run Freqtrade and store results through `lab/db.py`.
3. Confidence, book, mixture and research-loop modules derive decisions from stored trials and results. `ci/research_sync.py` carries research events into review work and roster PRs; it does not merge roster promotions.
4. `lab/markets/` records venue data, backfills history, links contracts and scores paper trades in separate market tables. Wallet and broker modules maintain their own balance and paper-execution records.
5. `web/app.py` reads Postgres using a SELECT-only deployment role. FastAPI renders Jinja templates, HTMX refreshes fragments, and `web/auth.py` handles Forgejo OAuth and signed cookies. The site exposes no trade controls.

## Storage and boundaries

`DATABASE_URL` is the connection source for [lab/db.py](../lab/db.py). Numbered SQL files in [lab/migrations](../lab/migrations/) are applied once by [lab/migrate.py](../lab/migrate.py), which records applied migrations. Database tests use a disposable migrated database and truncate tables through `lab/tests/conftest.py`.

Postgres holds research, snapshots and market records; Freqtrade uses per-bot sqlite state and filesystem candles/results. Those stores have different recovery needs; [deployment](operations/deploy.md) links their operational home. Credentials are supplied at runtime, outside tracked exchange configuration.

`lab/watcher.py` can stop bot entries; the read-only site cannot. Paper and live boundaries are enforced at their entry points, with the operating policy in [operating rules](operating-rules.md). `lab/healthcheck.py` accepts the bot API or site health endpoint for Docker; cluster probes are defined in home-config.

## Delivery

[image.yml](../.github/workflows/image.yml) runs secret scanning, static checks, database tests, strategy loading and ML tests. Master builds publish image digests and invoke `ci/bump_home_config.py`. Home-config owns workload commands, schedules, volumes, ingress, roles and secrets. See [deployment](operations/deploy.md) for that handoff and [standards](standards.md) for the check scope.
