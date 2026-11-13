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
