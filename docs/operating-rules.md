# Operating rules

Written before any real money, so no rule gets made up on a bad day. They bind the human who runs the bots as much as the code. Nothing trades money today (`docs/live.md`), and these rules hold for the paper league too, so the habits exist before the stakes do.

The reason is plain: the operator is the likeliest failure of an automated system. People drop an algorithm after one visible mistake, close winners early, buy what is in the news, and look more often the more it hurts.

## What the operator may do

Three actions, nothing else:

- **Pause.** Stop a bot's entries (`/stopentry`). Open trades keep their exits and their exchange stops.
- **Size down.** Lower the stake or the open-trade cap. Raising either is a stage change, which goes through `[[book.stages]]` and a PR.
- **Kill switch.** Stop the bot (`/stop`). It restarts only when a human calls `/start`, because a live bot has `initial_state: stopped`. Open trades keep their exchange stops.

## What the operator never does

- **No manual trade.** No forced entry, no forced exit, no order placed or closed by hand in the exchange UI, and no position added to. A bot that is wrong is paused, not overruled.
- **No parameter edit on a bot with open trades.** The strategy file, its params file, the pair list, the timeframe, the stoploss and the caps stay as they were at the last start until the bot is flat. Change them while it is flat, through a PR, and the change takes effect at the next start.
- **No edit during a drawdown.** A bad week is not evidence. Retiring a strategy needs the stated decay test (the "not decayed" gate and `retire` suggestion of `lab/confidence.py`, README "Decay test"), not one drawdown.
- **No promotion off the news.** A strategy goes to a bot through the trial register and `confidence.ready` only. A coin in the headlines or a hot week is not a reason.

## Cadence

- Read P&L once a week, on a fixed day. The daily loss watcher acts on its own and is not a prompt to decide anything.
- Judge a decision by whether these rules were followed over a full sample, not by last week's result.
- Log every override in the repo's issue or the lab notes with its date and reason, including a kill switch pull. An override with no reason in the log did not happen under the rules.

## What the code enforces

`lab/live.py` is the one entry point of a live bot, and it already refuses to start (`GUARD_LIFTED`). Once a human lifts the guard, it still exits 2 unless all of these hold:

| Rule | Enforcement | Test |
|---|---|---|
| No manual entry | `force_entry_enable` must be false, which stops `/forcelong`, `/forceenter` and `force_entry` over the bot API | `test_refuses_each_field`, `test_a_missing_field_is_refused` |
| No manual exit or entry by chat | Telegram must be off, since it carries `/forceexit` and `/forcelong` | `test_refuses_each_field` |
| No position growth | `position_adjustment_enable` must be false | `test_refuses_each_field` |
| No parameter edit over open trades | The start is refused when the bot's sqlite has an open trade and the strategy source, its params file, the venue, pairs, caps, stoploss, timeframe or order types differ from the last start | `test_an_edit_over_open_trades_is_refused_and_keeps_the_old_record`, `test_lifted_refuses_an_edited_strategy_over_open_trades` |
| Pause, size down, kill switch stay possible | The bot API must be on, so the watcher and the human can reach `/stopentry` and `/stop` | `test_refuses_each_field` |

## What the code cannot enforce

- **A trade placed in the exchange UI.** The exchange allows it, and the bot cannot tell. The reconciliation in `docs/live.md` catches it as a mismatch, and an unexplained mismatch demotes the strategy to Stage 0.
- **A bot API call with a valid login.** `/forceexit` is an API route that Freqtrade serves whenever the API is on. The watcher and a human hold the same credentials. The rule is the control here, and the override log is how it is checked.
- **An edit made while the bot is stopped but holding open trades.** The start check sees it at the next start and refuses, so the bot stays down until the edit is reverted or the trades are closed by their own exits.
- **The paper league.** `lab/league.py` has `force_entry_enable` false but does not check the rest, since paper trades cost nothing.
