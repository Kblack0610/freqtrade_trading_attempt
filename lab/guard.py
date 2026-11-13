"""The live guard: what every real-money entry point checks, whatever the engine or venue. Today it always refuses.

GUARD_LIFTED is False, and lifting it is a human's PR with the docs that state the rule (docs/live.md). With it lifted,
refusals() still names why a start may not trade money: a stake or open-position count outside the [live] stage of
lab/suggest.toml [[book.stages]], a venue outside [live] venues, a strategy not at ready, or the bankroll kill switch
that lab/watcher.py holds. An engine adds its own config checks on top: lab/live.py for a Freqtrade bot.
"""

GUARD_LIFTED = False


def stage_of(live: dict, book: dict) -> dict:
    """The [[book.stages]] row the [live] stage names: its capital is the bankroll across every venue."""
    return next(s for s in book["stages"] if s["name"] == live["stage"])


def killed(conn) -> str | None:
    """Why the bankroll is stopped, None while it is not. No row yet means no money account has been watched."""
    row = conn.execute("SELECT breached_at, reason FROM bankroll").fetchone()
    return row[1] if row and row[0] is not None else None


def refusals(venue: str | None, stake, slots, live: dict, stage: dict, ready: bool, stopped: str | None) -> list[str]:
    """Why this start may not trade money, empty when it may. Names fields, never values of keys."""
    problems = [] if GUARD_LIFTED else ["the no-real-money guard is on (docs/live.md)"]
    if not isinstance(stake, (int, float)) or not 0 < stake <= stage["stake"]:
        problems.append(f"stake_amount is {stake!r}, want a number up to {stage['stake']}")
    if not isinstance(slots, (int, float)) or not 0 < slots <= stage["max_open"]:
        problems.append(f"max_open_trades is {slots!r}, want 1 to {stage['max_open']}")
    if venue not in live["venues"]:
        problems.append(f"exchange {venue!r} is not one of {live['venues']}")
    if not ready:
        problems.append("the strategy is not at ready on this venue")
    if stopped:
        problems.append(f"the bankroll is stopped: {stopped}")
    return problems
