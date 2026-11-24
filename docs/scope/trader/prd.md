# Trader PRD

## Problem

Picking a trading strategy by a backtest alone overfits: a lucky run looks like an edge. The operator needs a way to find out whether any strategy has an edge at all before any real money goes near it, and to see that evidence in one place.

## Users

One: the operator, plus the agent sessions that work the trader board. There are no external users (`docs/support.md`).

## Scope

In:

- A paper-trading league: bots each run one strategy on one exchange in dry run (`lab/crypto/league.py`).
- A nightly analysis and a walk-forward that rank every strategy with confidence intervals, bias checks and return estimates.
- Event-contract and stock paper accounts on the same evidence standard.
- A read-only site showing results, coverage and accounts.

Out: real-money trading (guarded off, `docs/live.md`), a public product, user accounts beyond the operator's Forgejo sign-in.

## Requirements and acceptance criteria

1. AC1: no config with keys, or outside spot dry run, starts a bot. Check: `lab/tests/test_league.py`, `lab/tests/test_live.py`.
2. AC2: paper money adds up and every outbound client is on the money map. Check: `lab/tests/test_money.py`.
3. AC3: every nightly job runs by name from `lab/jobs.toml`. Check: `lab/tests/test_jobs.py`.
4. AC4: a ranking states its uncertainty, so a strategy is promoted only past the gates in `lab/crypto/suggest.toml`. Check: `lab/tests/` for `metrics` and `confidence`.
5. AC5: the site needs a Forgejo sign-in on every page but `/healthz` and offers no trade control. Check: `web/tests`.

## Success metrics

- The operator can answer "does anything here have an edge, and how sure are we" from the site.
- Calibration (`lab/crypto/calibration.py`) reports the gates' false pass rate and power, and the false pass rate stays at its set level.
- Nightly jobs finish; `/runs` shows no stale job.
