#!/usr/bin/env python3
"""Run one dry-run league bot.

A StatefulSet pod named <set>-<N> runs roster row N (blank lines and # comments skipped). A row is
`name exchange strategy [cohort overlay]`: the cohort is the league it reports in (the site and the exporter group
by it), and the overlay is a setup overlay id from lab/variants.toml, layered over the venue's config the way the
backtest runs it. A 3-column row is cohort `league`, overlay `base` (the strategy's own settings).
Before handing over to freqtrade it refuses anything that could trade real money:
the effective config (files plus FREQTRADE__ env overrides) must be dry run, spot, and carry
no exchange key or secret, and no FREQTRADE__ env var may be set at all.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

USER_DATA = Path(os.environ.get("FT_USER_DATA", "/repo/user_data"))
ROSTER = Path(os.environ.get("LEAGUE_ROSTER", "/etc/freqtrade-league/roster.tsv"))
STATE = Path(os.environ.get("LEAGUE_STATE", "/freqtrade/state"))
API_PORT = 8080
BASE = "base"  # the roster's word for no overlay; stored as '', as backtest_chunks.overlay is


class Row(NamedTuple):
    name: str
    exchange: str
    strategy: str
    cohort: str = "league"
    overlay: str = ""  # a lab/variants.toml overlay id, '' for none


def die(msg: str) -> None:
    print(f"league: {msg}", file=sys.stderr)
    sys.exit(2)


def roster_rows(path: Path) -> list[Row]:
    rows = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (3, 5):
            die(f"{path}:{n}: want 'name exchange strategy [cohort overlay]', got {raw!r}")
        if len(parts) == 5 and parts[4] == BASE:
            parts[4] = ""
        rows.append(Row(*parts))
    return rows


def overlay_config(overlay: str, exchange_cfg: Path) -> dict:
    """The freqtrade config a roster row's overlay layers over its venue, as lab/analyze.py builds it for a backtest."""
    from lab import variants

    known, _ = variants.load()
    if overlay not in known:
        die(f"overlay {overlay!r} is not in {variants.PATH}: known {', '.join(known) or 'none'}")
    whitelist = json.loads(exchange_cfg.read_text())["exchange"]["pair_whitelist"]
    try:
        return variants.config(known[overlay]["params"], whitelist)
    except ValueError as e:
        die(f"overlay {overlay!r}: {e}")


def ordinal() -> int:
    if "LEAGUE_INDEX" in os.environ:
        return int(os.environ["LEAGUE_INDEX"])
    m = re.search(r"-(\d+)$", os.environ.get("HOSTNAME", ""))
    if not m:
        die("no LEAGUE_INDEX and HOSTNAME has no -<ordinal> suffix")
    return int(m.group(1))


def guard(configs: list[Path]) -> None:
    """Refuse to start unless the effective config is spot dry run with no exchange credentials."""
    env_overrides = sorted(k for k in os.environ if k.startswith("FREQTRADE__"))
    if env_overrides:
        die(f"FREQTRADE__ env overrides are not allowed in the league: {env_overrides}")

    from freqtrade.configuration import Configuration

    cfg = Configuration.from_files([str(p) for p in configs])
    ex = cfg.get("exchange", {})
    problems = []
    if cfg.get("dry_run") is not True:
        problems.append("dry_run is not true")
    if cfg.get("trading_mode", "spot") != "spot":
        problems.append(f"trading_mode is {cfg.get('trading_mode')!r}, want spot")
    for field in ("key", "secret", "password", "uid", "private_key", "walletAddress"):
        if ex.get(field):
            problems.append(f"exchange.{field} is set")
    if problems:
        die("refusing to start: " + "; ".join(problems))


def main() -> None:
    rows = roster_rows(ROSTER)
    i = ordinal()
    if i >= len(rows):
        die(f"pod ordinal {i} has no roster row ({len(rows)} rows): set replicas to the row count")
    name, exchange, strategy, cohort, overlay = rows[i]

    exchange_cfg = USER_DATA / "exchanges" / f"{exchange}.json"
    if not exchange_cfg.is_file():
        die(f"roster row {i} ({name}) names exchange {exchange!r} but {exchange_cfg} does not exist")

    secrets = {k: os.environ.get(k, "") for k in ("FT_API_PASSWORD", "FT_API_JWT_SECRET", "FT_API_WS_TOKEN")}
    missing = [k for k, v in secrets.items() if not v]
    if missing:
        die(f"missing API secrets: {missing}")

    STATE.mkdir(parents=True, exist_ok=True)
    api_cfg = STATE / "api.json"
    api_cfg.write_text(json.dumps({
        "bot_name": name,
        "api_server": {
            "enabled": True,
            "listen_ip_address": "0.0.0.0",
            "listen_port": API_PORT,
            "verbosity": "error",
            "enable_openapi": False,
            "CORS_origins": [],
            "username": os.environ.get("FT_API_USERNAME", "freqtrader"),
            "password": secrets["FT_API_PASSWORD"],
            "jwt_secret_key": secrets["FT_API_JWT_SECRET"],
            "ws_token": secrets["FT_API_WS_TOKEN"],
        },
    }, indent=2))
    api_cfg.chmod(0o600)

    configs = [USER_DATA / "config.base.json", exchange_cfg]
    if overlay:
        overlay_cfg = STATE / "overlay.json"
        overlay_cfg.write_text(json.dumps(overlay_config(overlay, exchange_cfg), indent=2))
        configs.append(overlay_cfg)
    configs.append(api_cfg)
    guard(configs)

    print(f"league: row {i} -> {name}: {strategy} on {exchange}, cohort {cohort}, overlay {overlay or BASE} (dry run)",
          flush=True)
    args = ["freqtrade", "trade", "--userdir", str(USER_DATA), "--recursive-strategy-search",
            "--strategy", strategy, "--db-url", f"sqlite:///{STATE}/tradesv3.sqlite"]
    for c in configs:
        args += ["--config", str(c)]
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
