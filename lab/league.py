#!/usr/bin/env python3
"""Run one dry-run league bot.

A StatefulSet pod named <set>-<N> runs roster row N (blank lines and # comments skipped).
Before handing over to freqtrade it refuses anything that could trade real money:
the effective config (files plus FREQTRADE__ env overrides) must be dry run, spot, and carry
no exchange key or secret, and no FREQTRADE__ env var may be set at all.
"""

import json
import os
import re
import sys
from pathlib import Path

USER_DATA = Path(os.environ.get("FT_USER_DATA", "/repo/user_data"))
ROSTER = Path(os.environ.get("LEAGUE_ROSTER", "/etc/freqtrade-league/roster.tsv"))
STATE = Path(os.environ.get("LEAGUE_STATE", "/freqtrade/state"))
API_PORT = 8080


def die(msg: str) -> None:
    print(f"league: {msg}", file=sys.stderr)
    sys.exit(2)


def roster_rows(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            die(f"{path}:{n}: want 'name exchange strategy', got {raw!r}")
        rows.append((parts[0], parts[1], parts[2]))
    return rows


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
    name, exchange, strategy = rows[i]

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

    configs = [USER_DATA / "config.base.json", exchange_cfg, api_cfg]
    guard(configs)

    print(f"league: row {i} -> {name}: {strategy} on {exchange} (dry run)", flush=True)
    args = ["freqtrade", "trade", "--userdir", str(USER_DATA), "--recursive-strategy-search",
            "--strategy", strategy, "--db-url", f"sqlite:///{STATE}/tradesv3.sqlite"]
    for c in configs:
        args += ["--config", str(c)]
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
