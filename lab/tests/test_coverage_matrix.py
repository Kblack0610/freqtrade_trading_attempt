"""The market x stage table: every declared module exists, and the known gaps show as missing."""

import importlib.util

import pytest

from lab.stages import coverage

DECLARED = coverage.load()
CELLS = [(m, s, v["module"]) for m, by in DECLARED["markets"].items() for s, v in by.items()]


@pytest.mark.parametrize("market,stage,module", CELLS)
def test_declared_module_exists(market, stage, module):
    assert stage in DECLARED["stages"], f"{market}: unknown stage {stage}"
    assert importlib.util.find_spec(module), f"{market} {stage}: no module {module}"


def test_known_gaps_are_missing():
    by = {m["market"]: m["missing"] for m in coverage.matrix()["markets"]}
    assert by["stocks"] == ["gate", "live"]
    assert by["futures"] == ["gate", "live"]
    assert by["crypto"] == []


def test_a_market_with_no_declaration_is_all_missing():
    m = coverage.matrix({"stages": ["data", "paper"], "markets": {"x": {}}})["markets"][0]
    assert m["missing"] == ["data", "paper"]
