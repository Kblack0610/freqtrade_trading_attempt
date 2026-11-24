"""Event contracts and odds: venue adapters, the recorder and (later) the backtester and paper broker.

Kept out of the Freqtrade runtime: nothing under user_data/ imports it. Plan id-like-to-setup-abundant-seahorse;
venue facts in docs/markets.md.
"""

import os
import tomllib
from pathlib import Path

CONFIG = Path(os.environ.get("MARKETS_CONFIG", Path(__file__).with_name("markets.toml")))


def venues(path: Path = CONFIG) -> list[str]:
    """The event-contract venues, `[venues.*]` of lab/events/markets.toml: a trial under one of them is a betting try."""
    return list(tomllib.loads(path.read_text())["venues"])


def paper_venues(path: Path | None = None) -> list[str]:
    """The venues that run a paper league, `[paper] venues` of lab/events/markets.toml."""
    return tomllib.loads((path or CONFIG).read_text())["paper"]["venues"]


def sweep_ages(path: Path | None = None) -> tuple[int, dict[str, int]]:
    """Recorder freshness limits in minutes, shared by the site and exporter."""
    cfg = tomllib.loads((path or CONFIG).read_text())
    default = cfg["paper"]["max_book_age_min"]
    return default, {v: c.get("stale_after_min", default) for v, c in cfg["venues"].items()}


def quiet_limits(path: Path | None = None) -> dict[str, int]:
    """Hours each `[paper] venues` venue may go without a paper order before the volume watch calls its league
    stopped: its `[venues.<v>] quiet_after_h`, else `[paper] quiet_after_h`."""
    cfg = tomllib.loads((path or CONFIG).read_text())
    default = cfg["paper"]["quiet_after_h"]
    return {v: cfg["venues"].get(v, {}).get("quiet_after_h", default) for v in cfg["paper"]["venues"]}
