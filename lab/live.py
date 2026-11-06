#!/usr/bin/env python3
"""Run one live bot. Today it always refuses, and nothing deploys it.

GUARD_LIFTED is False, so main() exits 2 before it reads anything. Lifting it is a human's PR, with
the docs that state the rule in the same PR. After that the merged config must still pass check():
the caps of the [live] stage, a stop on the exchange, keys present, and a strategy at ready.
lab/league.py stays the entry point of every paper bot. See docs/live.md.
"""

import json
import os
import sys
from pathlib import Path

from lab import suggest
from lab.db import connect

GUARD_LIFTED = False

USER_DATA = Path(os.environ.get("FT_USER_DATA", "/repo/user_data"))
STATE = Path(os.environ.get("LIVE_STATE", "/freqtrade/state"))
KEYS = Path(os.environ.get("LIVE_KEYS", "/etc/trader-live/exchange.json"))
API_PORT = 8080


def die(msg: str) -> None:
    print(f"live: {msg}", file=sys.stderr)
    sys.exit(2)


def stage_of(live: dict, book: dict) -> dict:
    return next(s for s in book["stages"] if s["name"] == live["stage"])


def overlay(live: dict, stage: dict, name: str, api: dict) -> dict:
    """The config over base and exchange. Each field is explicit because a strategy's own value wins when the config has none."""
    return {
        "bot_name": name,
        "dry_run": False,
        "stake_amount": stage["stake"],
        "max_open_trades": stage["max_open"],
        "position_adjustment_enable": False,
        "force_entry_enable": False,
        # A restart waits for a human, so a killed bot cannot come back by itself. Stops stay on the exchange.
        "initial_state": "stopped",
        "order_types": live["order_types"],
        "exchange": {"pair_whitelist": live["pairs"]},
        "api_server": {
            "enabled": True,
            "listen_ip_address": "0.0.0.0",
            "listen_port": API_PORT,
            "verbosity": "error",
            "enable_openapi": False,
            "CORS_origins": [],
            **api,
        },
    }


def check(cfg: dict, live: dict, stage: dict, ready: bool) -> list[str]:
    """Why this merged config may not trade money, empty when it may. Names fields, never values of keys."""
    ex = cfg.get("exchange", {})
    stake, slots = cfg.get("stake_amount"), cfg.get("max_open_trades")
    problems = []
    if cfg.get("dry_run") is not False:
        problems.append("dry_run is not false")
    if cfg.get("trading_mode", "spot") != "spot" or cfg.get("margin_mode"):
        problems.append("not spot")
    if not isinstance(stake, (int, float)) or not 0 < stake <= stage["stake"]:
        problems.append(f"stake_amount is {stake!r}, want a number up to {stage['stake']}")
    if not isinstance(slots, (int, float)) or not 0 < slots <= stage["max_open"]:
        problems.append(f"max_open_trades is {slots!r}, want 1 to {stage['max_open']}")
    if cfg.get("position_adjustment_enable") is not False:
        problems.append("position_adjustment_enable is not false")
    if cfg.get("force_entry_enable") is not False:
        problems.append("force_entry_enable is not false")
    if cfg.get("initial_state") != "stopped":
        problems.append("initial_state is not stopped")
    if cfg.get("order_types", {}).get("stoploss_on_exchange") is not True:
        problems.append("no stop on the exchange")
    if not cfg.get("api_server", {}).get("enabled"):
        problems.append("api_server is off, so the watcher cannot stop this bot")
    if ex.get("name") not in live["venues"]:
        problems.append(f"exchange {ex.get('name')!r} is not one of {live['venues']}")
    if [p.get("method") for p in cfg.get("pairlists", [])] != ["StaticPairList"]:
        problems.append("pairlists is not one StaticPairList")
    pairs = ex.get("pair_whitelist") or []
    if not pairs or set(pairs) - set(live["pairs"]):
        problems.append(f"pair_whitelist is not inside {live['pairs']}")
    for field in ("key", "secret"):
        if not ex.get(field):
            problems.append(f"exchange.{field} is not set")
    if not ready:
        problems.append("the strategy is not at ready on this venue")
    return problems


def at_ready(conn, strategy: str, exchange: str) -> bool:
    row = conn.execute("SELECT ready FROM confidence WHERE strategy = %s AND exchange = %s",
                       (strategy, exchange)).fetchone()
    return bool(row and row[0])


def main() -> None:
    if not GUARD_LIFTED:
        die("refusing to start: the no-real-money guard is on (docs/live.md)")
    overrides = sorted(k for k in os.environ if k.startswith("FREQTRADE__"))
    if overrides:
        die(f"FREQTRADE__ env overrides are not allowed: {overrides}")
    name, strategy, exchange = (os.environ.get(k, "") for k in ("LIVE_BOT", "LIVE_STRATEGY", "LIVE_EXCHANGE"))
    api = {"username": os.environ.get("FT_API_USERNAME", "freqtrader"), "password": os.environ.get("FT_API_PASSWORD", ""),
           "jwt_secret_key": os.environ.get("FT_API_JWT_SECRET", ""), "ws_token": os.environ.get("FT_API_WS_TOKEN", "")}
    if not (name and strategy and exchange and all(api.values())):
        die("need LIVE_BOT, LIVE_STRATEGY, LIVE_EXCHANGE and the FT_API secrets")

    config = suggest.load_config()
    live = config["live"]
    stage = stage_of(live, config["book"])
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "live.json"
    path.touch(mode=0o600)
    path.write_text(json.dumps(overlay(live, stage, name, api), indent=2))
    # Keys come last, so check() sees whatever that file overrides.
    configs = [USER_DATA / "config.base.json", USER_DATA / "exchanges" / f"{exchange}.json", path, KEYS]

    from freqtrade.configuration import Configuration

    with connect() as conn:
        ready = at_ready(conn, strategy, exchange)
    problems = check(Configuration.from_files([str(p) for p in configs]), live, stage, ready)
    if problems:
        die("refusing to start: " + "; ".join(problems))

    print(f"live: {name}: {strategy} on {exchange}, REAL MONEY, {live['stage']}", flush=True)
    args = ["freqtrade", "trade", "--userdir", str(USER_DATA), "--recursive-strategy-search",
            "--strategy", strategy, "--db-url", f"sqlite:///{STATE}/tradesv3.sqlite"]
    for c in configs:
        args += ["--config", str(c)]
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
