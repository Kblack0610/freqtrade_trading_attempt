"""Setup overlays from lab/variants.toml: the account settings a strategy is backtested under besides its own.

load() expands each [overlays.<group>] grid (lab/grid.py) and refuses any key outside ALLOWED, so an overlay can
change how a backtest sizes and exits trades but never dry_run, the exchange or its keys. An overlay's id is the
first 8 hex digits of its params' content hash: the trial register, backtest_chunks.overlay and overlay_windows all
use it. config() turns one into the freqtrade config layered last, over config.base.json and the venue's file.
"""

import os
import tomllib
from pathlib import Path

from lab.grid import variant_of, variants

PATH = Path(os.environ.get("ANALYZE_VARIANTS", Path(__file__).with_name("variants.toml")))
ALLOWED = {"max_open_trades", "stake_amount", "stoploss", "minimal_roi", "pairs"}


def check(params: dict) -> None:
    bad = sorted(set(params) - ALLOWED)
    if bad:
        raise ValueError(f"an overlay may not set {', '.join(bad)}: only {', '.join(sorted(ALLOWED))}")
    if "pairs" in params and not (isinstance(params["pairs"], int) and params["pairs"] >= 1):
        raise ValueError(f"pairs is a count of whitelist pairs, not {params['pairs']!r}")
    if "stoploss" in params and not -1 < params["stoploss"] < 0:
        raise ValueError(f"stoploss is a negative ratio above -1, not {params['stoploss']!r}")
    stake = params.get("stake_amount", "unlimited")
    if stake != "unlimited" and not (isinstance(stake, (int, float)) and stake > 0):
        raise ValueError(f"stake_amount is a positive amount or \"unlimited\", not {stake!r}")


def load(path: Path | None = None) -> tuple[dict[str, dict], dict]:
    """(id -> {"group", "params"} in file order, [sweep]). An overlay two groups expand to is kept once, under the
    first; one that sets nothing is the base and is dropped."""
    cfg = tomllib.loads((path or PATH).read_text())
    out: dict[str, dict] = {}
    for group, grid in cfg.get("overlays", {}).items():
        for params in variants(grid):
            check(params)
            if params:
                out.setdefault(variant_of(params)[:8], {"group": group, "params": params})
    return out, cfg.get("sweep", {})


def config(params: dict, whitelist: list[str]) -> dict:
    """The freqtrade config an overlay layers over the venue's, for a venue whose whitelist is `whitelist`."""
    check(params)
    out = {k: v for k, v in params.items() if k != "pairs"}
    if "pairs" in params:
        out["exchange"] = {"pair_whitelist": whitelist[:params["pairs"]]}
    return out
