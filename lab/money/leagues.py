"""The one league scorecard: every paper league's accounts as the same columns (web/glossary.toml [[column]]).

`row` is the single place the columns are computed. Each league has a small adapter that maps its own rows into it,
so the definition of Return, Max drawdown and Watch lives here and not in three templates.
"""

import numpy as np

COLUMNS = ("account", "rule", "capital", "equity", "ret", "realized", "open", "fills", "closed", "max_drawdown",
           "watch", "marked_at")


def drawdown(capital: float, profits: list[float]) -> float | None:
    """Largest fall of capital plus the running sum of settled profits, as a fraction of the peak; None with no
    settled fill, since a flat curve has no fall to report."""
    if not profits or not capital:
        return None
    curve = np.concatenate([[capital], capital + np.cumsum(profits)])
    return float(np.max(1.0 - curve / np.maximum.accumulate(curve)))


def row(account: str, href: str, rule: str, capital: float | None, equity: float | None, realized: float | None,
        open_: int | None, fills: int | None, closed: int | None, max_drawdown: float | None, watch: str,
        marked_at) -> dict:
    ret = None if equity is None or not capital else equity / capital - 1
    return dict(zip(COLUMNS, (account, rule, capital, equity, ret, realized, open_, fills, closed, max_drawdown, watch,
                              marked_at), strict=True)) | {"href": href}


def watch_state(retired_at=None, stopped_at=None, breached_at=None, state: str | None = None) -> str:
    if retired_at:
        return "retired"
    if stopped_at:
        return "stopped"
    if breached_at:
        return "day limit hit"
    return state or "-"


def event_rows(accounts: list[dict], profits: dict[str, list[float]], rule_of) -> list[dict]:
    """The event-contract and stock leagues: paper_accounts rows (with fills and settled counts),
    each account's settled profits oldest first, and a function from (family, params) to the rule in words."""
    return [row(a["account"], "", rule_of(a["family"], a["params"]),
                a["capital"], a["equity"], a["realized"], a["open_positions"], a["fills"], a["settled"],
                drawdown(a["capital"], profits.get(a["account"], [])),
                watch_state(a["retired_at"], a["stopped_at"], a["breached_at"], a["state"]), a["marked_at"])
            for a in accounts]


def crypto_rows(bots: list[dict], watching: dict[str, dict], rule_of) -> list[dict]:
    """The crypto league: each bot's newest snapshot, the kill-switch row of its name, and a function from strategy
    to the rule in words. Capital is the balance less all profit, the balance the bot started with."""
    out = []
    for b in bots:
        reported = b["ts"] is not None and b["balance_total"] is not None
        w = watching.get(b["name"], {})
        dd = b["max_drawdown_pct"]
        out.append(row(b["name"], f"/bots/{b['name']}", rule_of(b["strategy"]),
                       b["balance_total"] - (b["profit_all_abs"] or 0) if reported else None,
                       b["balance_total"] if reported else None, b["profit_closed_abs"],
                       b["open_trades"], (b["closed_trades"] or 0) + (b["open_trades"] or 0) if reported else None,
                       b["closed_trades"], None if dd is None else dd / 100,
                       watch_state(None, w.get("stopped_at"), w.get("breached_at"), w.get("state")),
                       b["ts"]))
    return out
