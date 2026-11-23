# Changelog

Notable changes to the trader strategy repo, newest first. The image is built on each push to `master` and tagged `sha-<7>`; home-config pins it by digest, so an entry is live only after that bump. Per-change detail is the merged PR list (`git log --oneline`).

## Unreleased

### Added

- Beta stage documents: this changelog, privacy and terms (`docs/legal/`), a PRD and TSD (`docs/scope/trader/`), and a `.githooks/pre-push` that runs Ruff.

### Fixed

- Walk-forward: a hyperopt stopped at `WF_TIMEOUT` keeps its best epoch instead of failing the fold (#236).
- Inputs: `nws_cli` freshness threshold sits above a healthy reader's daily peak (#234).
- Markets: the S3 client waits out a stalled home disk (#233); bars history covers one year for Kalshi and Polymarket so the backfill drains (#232).
- Calibration: a resumed universe migrates its scratch schema (#230).
