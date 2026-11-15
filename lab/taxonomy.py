"""Signal categories are independent of the data a strategy reads."""

import tomllib
from pathlib import Path

CONFIG = Path(__file__).with_name("families.toml")


def load(path: Path = CONFIG) -> dict:
    cfg = tomllib.loads(path.read_text())
    for group in ("family_categories", "market_categories"):
        for name, category in cfg[group].items():
            validate(category, cfg)
    if cfg["families"].keys() != cfg["family_categories"].keys():
        raise ValueError("every method family needs a category")
    for tag in cfg["strategies"].values():
        if tag["family"] not in cfg["family_categories"]:
            raise ValueError(f"uncategorized family: {tag['family']}")
    return cfg


def validate(category: dict, cfg: dict | None = None) -> None:
    cfg = cfg if cfg is not None else load()
    if category.get("signal_source") not in cfg["signal_sources"]:
        raise ValueError("signal_source must name a configured signal source")
    if not category.get("instrument") or not category.get("horizon"):
        raise ValueError("category needs instrument and horizon")
