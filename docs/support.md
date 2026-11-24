# Support

Trader has no users, no store reviews and no in-app feedback. The one reader is the operator, so there is no support queue.

## Where problems land

- A bot, job or data problem: the watcher (`lab/money/watcher.py`) and the Sentinel alerts (for example `TraderFuturesStale`) page the operator. The `/runs` page shows each job's last result.
- A site or analysis fault found while reading: a row on the trader agent board (`notes ptask trader --agent`).
- A money-path mismatch: the watcher stops entries and pages the human (`docs/live.md`, reconciliation).

## Who reads it

The operator, and the agent sessions that work the trader board. If trader ever gains a second user, this page and `docs/brand.md` stop being enough.
