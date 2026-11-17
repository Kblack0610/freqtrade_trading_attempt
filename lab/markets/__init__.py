"""Event contracts and odds: venue adapters, the recorder and (later) the backtester and paper broker.

Kept out of the Freqtrade runtime: nothing under user_data/ imports it. Plan id-like-to-setup-abundant-seahorse;
venue facts in docs/markets.md.
"""

import os
import tomllib
from pathlib import Path

CONFIG = Path(os.environ.get("MARKETS_CONFIG", Path(__file__).resolve().parents[1] / "markets.toml"))


def venues(path: Path = CONFIG) -> list[str]:
    """The event-contract venues, `[venues.*]` of lab/markets.toml: a trial under one of them is a betting try."""
    return list(tomllib.loads(path.read_text())["venues"])


def paper_venues(path: Path | None = None) -> list[str]:
    """The venues that run a paper league, `[paper] venues` of lab/markets.toml."""
    return tomllib.loads((path or CONFIG).read_text())["paper"]["venues"]


def sweep_ages(path: Path | None = None) -> tuple[int, dict[str, int]]:
    """Recorder freshness limits in minutes, shared by the site and exporter."""
    cfg = tomllib.loads((path or CONFIG).read_text())
    default = cfg["paper"]["max_book_age_min"]
    return default, {v: c.get("stale_after_min", default) for v, c in cfg["venues"].items()}
