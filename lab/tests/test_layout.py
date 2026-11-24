"""The layout rule (README, Layout): the core never imports a market package, so a market is an adapter of the core and
never the other way round."""

import ast
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
MARKETS = ("crypto", "events", "sports", "stocks", "futures")


def lab_imports(path: Path) -> set[str]:
    """Every lab module a file names in an import, as its dotted path (`from lab import x` gives lab.x)."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.split(".")[0] == "lab"}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "lab":
            out |= {node.module} if node.module != "lab" else {f"lab.{a.name}" for a in node.names}
    return out


def test_core_imports_no_market_package():
    found = {f"{p.relative_to(LAB.parent)}: {m}" for p in (LAB / "core").glob("*.py") for m in lab_imports(p)
             if m.split(".")[1] in MARKETS}
    assert found == set()


def test_every_market_package_exists():
    """A renamed market package would let the rule above pass on nothing."""
    assert all((LAB / m / "__init__.py").is_file() for m in MARKETS)


def test_lab_imports_sees_a_market_import(tmp_path):
    """Negative control: the scanner catches both import forms."""
    f = tmp_path / "x.py"
    f.write_text("from lab import events\nfrom lab.crypto.analyze import main\nimport lab.sports.odds\n")
    assert lab_imports(f) == {"lab.events", "lab.crypto.analyze", "lab.sports.odds"}
