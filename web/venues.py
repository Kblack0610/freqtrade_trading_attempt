"""One /accounts row per venue (web/venues.toml): connected, error, stale or missing, and what the person does.

Each source is one query for the newest row per account, so the page costs four small queries however many venues there
are. A check's status is the worst of its matching accounts; a venue's is the worst of its checks, and a venue with no
checks is missing until code reads it.
"""

import datetime as dt
import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "venues.toml"
SOURCES = {
    "broker": "SELECT DISTINCT ON (broker) broker AS account, read_at AS at, error FROM broker_balances "
              "ORDER BY broker, read_at DESC",
    "drill": "SELECT DISTINCT ON (venue) venue AS account, placed_at AS at, error FROM demo_orders ORDER BY venue, placed_at DESC",
    "paper": "SELECT venue AS account, max(marked_at) AS at, NULL AS error FROM paper_accounts GROUP BY venue",
    "wallet": "SELECT DISTINCT ON (account) account AS account, read_at AS at, error FROM wallet_balances "
              "ORDER BY account, read_at DESC",
}
WORST = ("missing", "error", "stale", "connected")  # first is worst


def load() -> list[dict]:
    return tomllib.loads(CONFIG.read_text())["venue"]


def matches(account: str, match: str) -> bool:
    return account.startswith(match[:-1]) if match.endswith("*") else account == match


def check(c: dict, found: list[dict], now: dt.datetime) -> dict:
    hits = [r for r in found if matches(r["account"], c["match"])]
    if not hits:
        return {**c, "status": "missing", "at": None, "error": None}
    newest = max(hits, key=lambda r: r["at"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    errors = [f"{r['account']}: {r['error']}" for r in hits if r["error"]]
    if errors:
        status = "error"
    elif newest["at"] is None or now - newest["at"] > dt.timedelta(hours=c["max_age_h"]):
        status = "stale"
    else:
        status = "connected"
    return {**c, "status": status, "at": newest["at"], "error": "; ".join(errors) or None}


def rows(query, now: dt.datetime | None = None) -> list[dict]:
    """query(sql) -> list of dicts. Every venue in the toml, in its order."""
    now = now or dt.datetime.now(dt.timezone.utc)
    venues = load()
    found = {s: query(SOURCES[s]) for s in sorted({c["source"] for v in venues for c in v["checks"]})}
    out = []
    for v in venues:
        checks = [check(c, found[c["source"]], now) for c in v["checks"]]
        status = min((c["status"] for c in checks), key=WORST.index, default="missing")
        out.append({**v, "checks": checks, "status": status})
    return out
