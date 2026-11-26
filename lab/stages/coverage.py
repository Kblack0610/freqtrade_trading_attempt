"""The market x stage table /coverage shows, read from lab/stages.toml. A missing stage is a gap, never a blank."""

import tomllib
from pathlib import Path

STAGES_TOML = Path(__file__).resolve().parent.parent / "stages.toml"


def load(path: Path = STAGES_TOML) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# The row count behind a declared stage's `rows` table, for the stage's `key`.
EVIDENCE = {"market_backtests": "SELECT count(*) AS n FROM market_backtests WHERE venue = %s",
            "readiness": "SELECT count(*) AS n FROM readiness WHERE market = %s"}


def matrix(declared: dict | None = None, count=None) -> dict:
    """{"stages": [...], "markets": [{"market", "cells": [{"stage", "module", "note"} or {"stage", "module": None}], "missing": [...]}]}.
    With `count(table, key) -> int`, a stage declaring `rows` whose table has none for its key is missing, its note naming the module."""
    declared = declared or load()
    stages = declared["stages"]
    markets = []
    for market, by_stage in declared["markets"].items():
        cells = [{"stage": s, "module": None, "note": None, **by_stage.get(s, {})} for s in stages]
        for c in cells:
            if count and c["module"] and "rows" in c and not count(c["rows"], c["key"]):
                c.update(module=None, note=f"{c['module']} has written no rows")
        markets.append({"market": market, "cells": cells, "missing": [c["stage"] for c in cells if not c["module"]]})
    return {"stages": stages, "markets": markets}
