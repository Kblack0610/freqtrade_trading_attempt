# Changelog

Notable changes to the trader strategy repo, newest first. The image is built on each push to `master` and tagged `sha-<7>`; home-config pins it by digest, so an entry is live only after that bump. Per-change detail is the merged PR list (`git log --oneline`).

## Unreleased

### Added

- `/desk` page: lot-size notes, the best dry run of each recent day and the weather bets on one page (ar-3306).
- Beta stage documents: this changelog, privacy and terms (`docs/legal/`), a PRD and TSD (`docs/scope/trader/`), and a `.githooks/pre-push` that runs Ruff.

### Fixed

- CI: the home-config digest bump retries its merge while Forgejo answers 405 (required checks not passed yet), for up to 15 min. Since home-config's master began requiring `checks / *` (2026-11-25 04:19Z), every bump's merge answered 405 and nothing deployed, so the keepalive fix (ar-5629) stayed off the cluster (ar-5150).
- Database: every lab connection and the web pool set TCP keepalives (`lab/core/db.py` `KEEPALIVE`), so a job whose Postgres vanishes mid-query (asus-laptop rebooted 2026-11-25 07:04Z) fails in about a minute instead of hanging to its Job deadline and stalling the recorder (ar-5629).
- CI: the quality job installs its static-check tools from the hash-pinned `lab/requirements-dev.lock`, so mypy and ruff's dependencies no longer float between runs (tr-321).
- Markets: `max_open` fills an account's entries best edge first, not oldest quote first, and keeps the best two it turned away per pass as `capped` decisions (tr-429).
- Walk-forward: a hyperopt stopped at `WF_TIMEOUT` keeps its best epoch instead of failing the fold (#236).
- Inputs: `nws_cli` freshness threshold sits above a healthy reader's daily peak (#234).
- Markets: the S3 client waits out a stalled home disk (#233); bars history covers one year for Kalshi and Polymarket so the backfill drains (#232).
- Calibration: a resumed universe migrates its scratch schema (#230).
