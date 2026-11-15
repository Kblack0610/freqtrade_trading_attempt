"""The site's vocabulary (web/glossary.toml), read for the guide's glossary and the scorecard's column help."""

import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "glossary.toml"


def load() -> dict:
    return tomllib.loads(CONFIG.read_text())
