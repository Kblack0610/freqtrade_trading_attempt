"""Where each venue's pair list came from, so a result says whether it was chosen with hindsight.

user_data/universe.json is written by lab/pairs.py next to the whitelist it picks (never inside the exchange file:
that file's hash keys the backtest cache). A venue's list is `point-in-time` only if it was picked from the pairs
liquid at the time each year is tested; Freqtrade backtests one fixed whitelist, so every list pairs.py writes is
`current-listing`, today's liquid survivors. A whitelist with no record, or one edited since its record, counts as
current-listing too: nothing else is known about it.
"""

import json
from pathlib import Path

POINT_IN_TIME = "point-in-time"
CURRENT_LISTING = "current-listing"


def load(user_data: Path) -> dict:
    path = user_data / "universe.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def save(user_data: Path, exchange: str, entry: dict) -> Path:
    path = user_data / "universe.json"
    book = load(user_data)
    book[exchange] = entry
    path.write_text(json.dumps(book, indent=2, sort_keys=True) + "\n")
    return path


def describe(user_data: Path, exchange: str, whitelist: list[str]) -> dict:
    """The record stored on a run: source, when the list was picked, its floor, and whether the record is trusted."""
    entry = load(user_data).get(exchange)
    if not entry or sorted(entry["pairs"]) != sorted(whitelist):
        return {"source": CURRENT_LISTING, "recorded": False, "pairs": len(whitelist)}
    return {"source": entry["source"], "recorded": True, "pairs": len(whitelist), "as_of": entry["as_of"],
            "listed_by": entry["listed_by"], "min_volume": entry["min_volume"]}


def describe_all(user_data: Path, exchanges: list[str]) -> dict:
    out = {}
    for ex in exchanges:
        cfg = json.loads((user_data / "exchanges" / f"{ex}.json").read_text())
        out[ex] = describe(user_data, ex, cfg["exchange"]["pair_whitelist"])
    return out
