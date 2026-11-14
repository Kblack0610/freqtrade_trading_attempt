"""The overview's lane cards (web/lanes.toml): one per lane, a big counted number in its state hue, labelled parts and
up to three rows to look at first.

A card counts nothing of its own: it maps rows the lane's tab already reads (web/app.py passes them in), under the
tab's own staleness rule, so card and tab cannot disagree. Levels are red (broken), amber (needs a look), nodata
(nothing to count yet, never green) and green (counted and fine); cards sort in that order, toml order within a level.
"""

import datetime as dt
import tomllib
from collections.abc import Callable
from pathlib import Path

from lab import calls as calls_lab

CONFIG = Path(__file__).resolve().parent / "lanes.toml"
LEVELS = ("red", "amber", "nodata", "green")  # first is worst
SHOWN_FIRST = 3


def load() -> list[dict]:
    return tomllib.loads(CONFIG.read_text())["lane"]


def usd(v: float) -> str:
    return f"{v:,.2f} USD"


def part(label: str, value, level: str = "") -> dict:
    return {"label": label, "value": str(value), "level": level}


def row(text: str, sub: str, level: str, href: str = "", at: dt.datetime | None = None) -> dict:
    return {"text": text, "sub": sub, "level": level, "href": href, "at": at}


def old(t: dt.datetime | None, now: dt.datetime, hours: float) -> bool:
    return t is None or now - t > dt.timedelta(hours=hours)


def card(big, unit: str, level: str, counted: str, parts: list | None = None, first: list | None = None) -> dict:
    return {"big": str(big), "unit": unit, "level": level, "counted": counted, "parts": parts or [],
            "first": (first or [])[:SHOWN_FIRST], "more": max(0, len(first or []) - SHOWN_FIRST)}


def empty(unit: str, counted: str) -> dict:
    return card("-", unit, "nodata", counted)


def crypto(read: dict, now: dt.datetime, cfg: dict) -> dict:
    bots = read["bots"]
    if not bots:
        return empty("no bots on the roster", "bots table: no rows")
    live = [b for b in bots if b["ts"] and b["dry_run"] is False]
    down = [b for b in bots if b["error"]]
    late = [b for b in bots if not b["error"] and old(b["ts"], now, read["stale_h"])]
    fresh = len(bots) - len(down) - len(late)
    first = [row(b["name"], "not dry run", "red", f"/bots/{b['name']}", b["ts"]) for b in live]
    first += [row(b["name"], f"unreachable: {b['error']}", "amber", f"/bots/{b['name']}", b["ts"]) for b in down]
    first += [row(b["name"], "no snapshot" if not b["ts"] else "last snapshot", "amber", f"/bots/{b['name']}", b["ts"])
              for b in late]
    snaps = [b for b in bots if b["ts"] and b["profit_all_abs"] is not None]
    parts = [part("open trades", sum(b["open_trades"] or 0 for b in snaps)),
             part("closed trades", sum(b["closed_trades"] or 0 for b in snaps)),
             part("paper P&L", usd(sum(b["profit_all_abs"] for b in snaps)))]
    parts += [part("stale", len(late), "amber")] if late else []
    parts += [part("unreachable", len(down), "amber")] if down else []
    parts += [part("not dry run", len(live), "red")] if live else []
    level = "red" if live else "amber" if down or late else "green"
    return card(fresh, f"of {len(bots)} bots reporting", level,
                f"each bot's newest snapshot; stale past {read['stale_h']:g} h", parts, first)


def betting(read: dict, now: dt.datetime, cfg: dict) -> dict:
    accounts = read["paper"]
    if not accounts:
        return empty("no paper accounts", "paper_accounts: no rows")
    ages = read["paper_max_age_h"]
    late = [a for a in accounts if old(a["marked_at"], now, ages.get(a["venue"], read["stale_h"]))]
    marked = [a for a in accounts if a["marked_at"] and a["equity"] is not None]
    venues = sorted({a["venue"] for a in accounts})
    parts = [part("venues", ", ".join(venues)), part("fills", sum(a["fills"] for a in accounts)),
             part("settled", sum(a["settled"] for a in accounts)),
             part("open positions", sum(a["open_positions"] or 0 for a in marked)),
             part("paper P&L", usd(sum(a["equity"] - a["capital"] for a in marked)))]
    parts += [part("stale", len(late), "amber")] if late else []
    first = [row(a["account"], "never marked" if not a["marked_at"] else "last marked", "amber", "/markets/book",
                 a["marked_at"]) for a in late]
    return card(len(accounts) - len(late), f"of {len(accounts)} accounts marked", "amber" if late else "green",
                "each account's newest mark; stale past its venue's paper age in web/venues.toml", parts, first)


def shadow(read: dict, now: dt.datetime, cfg: dict) -> dict:
    by_state: dict[str, int] = {}
    for r in read["shadow_counts"]:
        by_state[r["state"]] = by_state.get(r["state"], 0) + r["n"]
    if not by_state:
        return empty("no shadow accounts yet", "shadow_accounts: no rows")
    newest = read["shadow_newest"]
    parts = [part("winning", by_state.get("winning", 0)), part("trial", by_state.get("trial", 0)),
             part("retired", by_state.get("retired", 0)), part("newest mark", newest.isoformat() if newest else "none")]
    return card(by_state.get("winning", 0) + by_state.get("trial", 0), "active accounts", "green",
                "shadow_accounts by state; active is trial plus winning", parts)


def calls(read: dict, now: dt.datetime, cfg: dict) -> dict:
    found = read["calls"]
    if not found:
        return empty("no calls recorded", "calls: no rows")
    slack, today = read["calls_cfg"]["slack_days"], now.date()
    scored = waiting = 0
    first = []
    for c in found:
        for h in c["horizons"]:
            if str(h) in c["scores"]:
                scored += 1
            elif calls_lab.overdue(c["entry_session"], h, today, slack):
                first.append(row(f"{c['ticker']} {c['direction']}", f"{h}-session horizon unscored", "red", "/calls"))
            else:
                waiting += 1
    parts = [part("horizons scored", scored), part("waiting", waiting)]
    parts += [part("overdue", len(first), "red")] if first else []
    return card(len(found), "calls recorded", "red" if first else "green",
                f"every call and horizon; overdue past its span plus {slack} days", parts, first)


def venues(read: dict, now: dt.datetime, cfg: dict) -> dict:
    found = read["venues"]
    if not found:
        return empty("no venues", "web/venues.toml: no venues")
    n = {s: [v for v in found if v["status"] == s] for s in ("connected", "error", "stale", "missing")}
    parts = [part("connected", len(n["connected"]))]
    parts += [part("error", len(n["error"]), "red")] if n["error"] else []
    parts += [part("stale", len(n["stale"]), "amber")] if n["stale"] else []
    parts += [part("not read yet", len(n["missing"]))] if n["missing"] else []
    first = [row(v["name"], next((c["error"] for c in v["checks"] if c["error"]), "error"), "red", "/accounts")
             for v in n["error"]]
    first += [row(v["name"], "stale read or drill", "amber", "/accounts", max((c["at"] for c in v["checks"] if c["at"]),
                                                                           default=None)) for v in n["stale"]]
    first += [row(v["name"], v.get("human", ""), "", "/accounts") for v in n["missing"]]
    level = "red" if n["error"] else "amber" if n["stale"] else "green" if n["connected"] else "nodata"
    return card(len(n["connected"]), f"of {len(found)} venues connected", level,
                "web/venues.toml checks: a recent read or drill with no error", parts, first)


def recorder(read: dict, now: dt.datetime, cfg: dict) -> dict:
    found = read["recorder"]
    if not found:
        return empty("no venues recorded", "venues: no rows")
    max_min, ages = read["recorder_ages"]
    swept = [v for v in found if v["started_at"]]
    late = [v for v in swept if old(v["last_ok"], now, ages.get(v["venue"], max_min) / 60)]
    failing = sum(v["failed_1d"] for v in found)
    parts = [part("failed sweeps, 1 d", failing, "amber" if failing else "")]
    parts += [part("stale", len(late), "amber")] if late else []
    parts += [part("never swept", len(found) - len(swept))] if len(swept) < len(found) else []
    first = [row(v["venue"], f"sweep failed: {v['error']}" if v["ok"] is False else "last good sweep" if v["last_ok"] else "no good sweep", "amber",
                 "/markets", v["last_ok"]) for v in late]
    level = "amber" if late or failing else "green" if swept else "nodata"
    return card(len(swept) - len(late), f"of {len(found)} venues fresh", level,
                f"each venue's newest good sweep; stale past {max_min} min or its stale_after_min (lab/markets.toml)", parts, first)


def prices(read: dict, now: dt.datetime, cfg: dict) -> dict:
    found = read["coverage"]
    if not found:
        return empty("no candles yet", "data_coverage: no rows")
    late = [c for c in found if old(c["last_ts"], now, read["stale_h"])]
    parts = [part("series", sum(c["series"] for c in found)), part("full history", sum(c["at_oldest"] for c in found))]
    parts += [part("stale", len(late), "amber")] if late else []
    first = [row(c["exchange"], "newest candle" if c["last_ts"] else "no candles", "amber", "/data", c["last_ts"]) for c in late]
    return card(len(found) - len(late), f"of {len(found)} exchanges fresh", "amber" if late else "green",
                f"each exchange's newest candle; stale past {read['stale_h']:g} h", parts, first)


def research(read: dict, now: dt.datetime, cfg: dict) -> dict:
    runs, crypto_, betting_ = read["runs"], read["confidence"], read["readiness"]
    if not runs and not crypto_["n"] and not betting_["n"]:
        return empty("no analysis has run", "runs, confidence, market_readiness: no rows")
    parts = [part("crypto pairs ready", f"{crypto_['ready']} of {crypto_['n']}"),
             part("betting variants ready", f"{betting_['ready']} of {betting_['n']}"),
             part("suggestions", ", ".join(f"{k['n']} {k['kind']}" for k in read["suggestions"]) or "none")]
    newest = runs[0] if runs else None
    good = next((r for r in runs if r["ok"] and r["finished_at"]), None)
    first, level = [], "green"
    if newest is None:
        level = "nodata"
    elif newest["finished_at"] and newest["ok"] is False:
        first, level = [row(f"analysis {newest['name']}", "failed", "red", f"/runs/{newest['id']}",
                            newest["finished_at"])], "red"
    elif good is None or old(good["finished_at"], now, cfg["max_age_h"]):
        if newest is not good:
            first.append(row(f"analysis {newest['name']}", "started, not finished", "amber", f"/runs/{newest['id']}",
                             newest["started_at"]))
        first.append(row("last good analysis", good["name"] if good else "none", "amber",
                         f"/runs/{good['id']}" if good else "/runs", good["finished_at"] if good else None))
        level = "amber"
    return card(crypto_["ready"] + betting_["ready"], "pass every gate", level,
                f"confidence and market_readiness rows with ready set; the newest good analysis, stale past "
                f"{cfg['max_age_h']} h", parts, first)


def money(read: dict, now: dt.datetime, cfg: dict) -> dict:
    bank, watch = read["bankroll"], read["watch"]
    live = [b for b in read["bots"] if b["ts"] and b["dry_run"] is False]
    stopped = [w for w in watch if w["stopped_at"] or w["breached_at"]]
    first = [row(b["name"], "not dry run", "red", f"/bots/{b['name']}", b["ts"]) for b in live]
    if bank and bank["breached_at"]:
        first.append(row("bankroll", f"stopped: {bank['reason']}", "red", "/book", bank["breached_at"]))
    first += [row(w["bot"], "entries stopped" if w["stopped_at"] else "loss limit breached", "amber", "/accounts",
                  w["stopped_at"] or w["breached_at"]) for w in stopped]
    parts = [part("guard", "lifted" if read["guard_lifted"] else "locked"),
             part("money accounts", bank["accounts"] if bank else 0),
             part("watched", len(watch))]
    parts += [part("stopped", len(stopped), "amber")] if stopped else []
    level = "red" if live or (bank and bank["breached_at"]) else "amber" if stopped else "green"
    if read["guard_lifted"] and bank:
        big, unit = f"{bank['equity']:,.0f}", f"USD equity of a {bank['cap']:,.0f} USD cap"
    else:
        big, unit = ("Dry run", "no real money can trade") if not read["guard_lifted"] else ("-", "bankroll not checked")
    return card(big, unit, level, "lab/guard.py GUARD_LIFTED, the bankroll row and every watch row", parts, first)


READERS: dict[str, Callable[[dict, dt.datetime, dict], dict]] = {
    "crypto": crypto, "betting": betting, "shadow": shadow, "calls": calls, "venues": venues, "recorder": recorder,
    "prices": prices, "research": research, "money": money,
}


def cards(read: dict, now: dt.datetime | None = None) -> list[dict]:
    """Every lane in web/lanes.toml as a card, red then amber then no data then green, toml order within each."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = [{**lane, **READERS[lane["id"]](read, now, lane)} for lane in load()]
    return sorted(out, key=lambda c: LEVELS.index(c["level"]))
