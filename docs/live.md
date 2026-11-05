# Live bots

Nothing here trades money. `lab/live.py` always refuses, and no manifest starts it. This page says what it would check, what a human changes to lift the guard, and how a live bot would be reconciled and backed up.

## What `lab/live.py` checks

`GUARD_LIFTED` is False, so `main()` exits 2 before it reads a config, a key or the database. With the guard lifted, it builds the config from the stage named in `[live]` of `lab/suggest.toml` and exits 2 unless all of these hold on the merged config:

- Real orders, spot, no margin.
- Stake and open trades are numbers within the stage. `unlimited`, -1 and infinity are refused.
- `position_adjustment_enable` and `force_entry_enable` are false, so a position cannot grow past its stake.
- `initial_state` is `stopped`.
- `stoploss_on_exchange` is true.
- The bot API is on, so the watcher can stop the bot.
- The venue is in `[live] venues`, the pair list is one `StaticPairList`, and every pair is in `[live] pairs`.
- Key and secret are set, and no `FREQTRADE__` env var is.
- `confidence.ready` is true for this strategy on this venue.

Stake and open trades have one home, `[[book.stages]]`, which `/book` reads too. The keys file loads last, and the checks run after it, so it cannot raise a cap.

## Lifting the guard

This is a human decision, made in one PR, and only after a strategy reaches ready and the Accounts and keys phase passes:

1. Set `GUARD_LIFTED = True` and change `test_the_guard_is_on`.
2. Change the docs that state the rule: `docs/idea.md` in the lab project, the anchor, the running plan and the guard section of the README.
3. Give `lab/ingest.py` and the not-dry-run alert their exception for the live namespace. Both refuse a live bot today.
4. Add `apps/freqtrade-live/` in home-config. `lab/league.py` stays as it is, and it refuses this config.

## What these checks do not cover

- **A strategy's own stop.** `strategies` holds no stoploss, so a wide stop passes here. The Open loss gate in the score is the only limit on it.
- **A restart.** `initial_state: stopped` means a restarted bot runs no exit logic until a human calls `/start`. Open trades keep their exchange stops and nothing else. That is the price of a killed bot staying down.
- **A gap on Binance.US.** Its exchange stop is limit only, so a fast fall can pass through it. Kraken's limit stop has the same risk, and it also offers a market stop.
- **The narrower pair list.** Scores come from ten pairs and `[live] pairs` holds four. Re-run the strategy on those four before funding it.
- **Liquidity over time.** The four pairs come from one 24-hour reading on 2026-11-05. Binance.US BTC/USD traded $3.6M that day and Kraken $384M. Measure again before Stage 1.

## Reconciliation

The exchange is the source of truth. The bot's sqlite is its working notes.

**What to compare,** using the read key and the bot API:

| Exchange | Bot | Mismatch when |
|---|---|---|
| Balance of each coin | Sum of open trade amounts | They differ by more than dust, including a coin held with no open trade |
| Open orders | Open orders on open trades | The exchange has an order the bot does not, or the reverse |
| Stop order per position | `stoploss_on_exchange` order id | A position has no live stop |
| USD balance | Wallet less open stakes | Difference is above the fees of the day |

**When.** Before any human calls `/start`, and on each watcher pass once a read key exists. An order can fill between the two reads, so a mismatch counts only when it survives two passes.

**On a mismatch.** The watcher stops entries and pages the human. No automation edits sqlite or places an order to fix it. A human finds the cause and acts in the exchange UI. An unexplained mismatch demotes the strategy to Stage 0, per the plan's demotion row.

This needs the read key, so it gets built in the Accounts and keys phase. The watcher reads only the bot today.

## Backups

P1 chose not to back up bot sqlite, because a loss only restarted a dry run. For a live bot it is the record of which position belongs to which trade.

- Bot sqlite gets an online `.backup` every hour and on each stop, copied off the node, because a local-path PVC lives on one disk.
- `freqtrade-ingest` already copies each trade with its raw JSON to Postgres every hour. Whether the shared Postgres backup covers `trader` is not verified: confirm it before Stage 1.
- Orders, fills and fees are exported from the exchange monthly. That ledger is the tax record, and it survives losing everything else.
- Keys stay in SOPS in home-config. No backup holds them.

**If sqlite is lost,** do not restore blind. Keep the bot stopped, compare the newest backup with the exchange, and start from the backup only if every position matches. Otherwise close the positions in the exchange UI and start on an empty database. `bot_trades` keys on `open_date`, so the old life's trades stay in Postgres.
