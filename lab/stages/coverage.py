"""The market x stage table /coverage shows, read from lab/stages.toml. A missing stage is a gap, never a blank."""

import tomllib
from pathlib import Path

STAGES_TOML = Path(__file__).resolve().parent.parent / "stages.toml"


def load(path: Path = STAGES_TOML) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def matrix(declared: dict | None = None) -> dict:
    """{"stages": [...], "markets": [{"market", "cells": [{"stage", "module", "note"} or {"stage", "module": None}], "missing": [...]}]}."""
    declared = declared or load()
    stages = declared["stages"]
    markets = []
    for market, by_stage in declared["markets"].items():
        cells = [{"stage": s, "module": None, "note": None, **by_stage.get(s, {})} for s in stages]
        markets.append({"market": market, "cells": cells, "missing": [c["stage"] for c in cells if not c["module"]]})
    return {"stages": stages, "markets": markets}
