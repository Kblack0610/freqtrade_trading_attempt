#!/usr/bin/env python3
"""Run one live bot. Today it always refuses, and nothing deploys it.

lab/guard.py's GUARD_LIFTED is False, so main() exits 2 before it reads anything. Lifting it is a human's PR, with
the docs that state the rule in the same PR. After that the merged config must still pass check(): the generic guard
of lab/guard.py (stage caps, venue, ready, the bankroll kill switch) and this engine's own, a stop on the exchange
and keys present among them.
It also holds the operator rules of docs/operating-rules.md: no force entry, no Telegram, and no start over open trades after an edit.
lab/league.py stays the entry point of every paper bot. See docs/live.md.
"""

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from lab import guard, suggest
from lab.db import connect

USER_DATA = Path(os.environ.get("FT_USER_DATA", "/repo/user_data"))
STATE = Path(os.environ.get("LIVE_STATE", "/freqtrade/state"))
KEYS = Path(os.environ.get("LIVE_KEYS", "/etc/trader-live/exchange.json"))
API_PORT = 8080


def die(msg: str) -> None:
    print(f"live: {msg}", file=sys.stderr)
    sys.exit(2)


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


def check(cfg: dict, live: dict, stage: dict, ready: bool, stopped: str | None) -> list[str]:
    """Why this merged config may not trade money, empty when it may: lab/guard.py's refusals, then Freqtrade's own.
    Names fields, never values of keys."""
    ex = cfg.get("exchange", {})
    problems = guard.refusals(ex.get("name"), cfg.get("stake_amount"), cfg.get("max_open_trades"), live, stage, ready,
                              stopped)
    if cfg.get("dry_run") is not False:
        problems.append("dry_run is not false")
    if cfg.get("trading_mode", "spot") != "spot" or cfg.get("margin_mode"):
        problems.append("not spot")
    if cfg.get("position_adjustment_enable") is not False:
        problems.append("position_adjustment_enable is not false")
    if cfg.get("force_entry_enable") is not False:
        problems.append("force_entry_enable is not false")
    if cfg.get("telegram", {}).get("enabled"):
        problems.append("telegram is on, and it carries /forceexit")
    if cfg.get("initial_state") != "stopped":
        problems.append("initial_state is not stopped")
    if cfg.get("order_types", {}).get("stoploss_on_exchange") is not True:
        problems.append("no stop on the exchange")
    if not cfg.get("api_server", {}).get("enabled"):
        problems.append("api_server is off, so the watcher cannot stop this bot")
    if [p.get("method") for p in cfg.get("pairlists", [])] != ["StaticPairList"]:
        problems.append("pairlists is not one StaticPairList")
    pairs = ex.get("pair_whitelist") or []
    if not pairs or set(pairs) - set(live["pairs"]):
        problems.append(f"pair_whitelist is not inside {live['pairs']}")
    for field in ("key", "secret"):
        if not ex.get(field):
            problems.append(f"exchange.{field} is not set")
    return problems


def open_trades(db: Path) -> int:
    """Open trades in the bot's sqlite, read-only. A bot that never ran has no file and no trades."""
    if not db.exists():
        return 0
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        return c.execute("SELECT count(*) FROM trades WHERE is_open = 1").fetchone()[0]


def fingerprint(cfg: dict, strategy: str, user_data: Path) -> str:
    """What the bot trades with: the strategy source and its params file, the venue, pairs and caps. Never a key or a secret."""
    parts = {k: cfg.get(k) for k in ("stake_amount", "max_open_trades", "timeframe", "stoploss", "minimal_roi", "order_types")}
    parts |= {"strategy": strategy, "exchange": cfg.get("exchange", {}).get("name"),
              "pairs": sorted(cfg.get("exchange", {}).get("pair_whitelist", []))}
    files = sorted(f for f in (user_data / "strategies").rglob("*") if f.is_file() and f.stem == strategy and f.suffix in (".py", ".json"))
    parts["files"] = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def edited_while_open(state: Path, fp: str, trades: int) -> bool:
    """True when a bot holds open trades and what it trades with differs from its last start. The record is rewritten only when it may be."""
    record = state / "started.json"
    before = json.loads(record.read_text())["fingerprint"] if record.exists() else None
    if trades and before not in (None, fp):
        return True
    record.write_text(json.dumps({"fingerprint": fp}))
    return False


def at_ready(conn, strategy: str, exchange: str) -> bool:
    row = conn.execute("SELECT ready FROM confidence WHERE strategy = %s AND exchange = %s",
                       (strategy, exchange)).fetchone()
    return bool(row and row[0])


def main() -> None:
    if not guard.GUARD_LIFTED:
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
    stage = guard.stage_of(live, config["book"])
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "live.json"
    path.touch(mode=0o600)
    path.write_text(json.dumps(overlay(live, stage, name, api), indent=2))
    # Keys come last, so check() sees whatever that file overrides.
    configs = [USER_DATA / "config.base.json", USER_DATA / "exchanges" / f"{exchange}.json", path, KEYS]

    from freqtrade.configuration import Configuration

    with connect() as conn:
        ready, stopped = at_ready(conn, strategy, exchange), guard.killed(conn)
    merged = Configuration.from_files([str(p) for p in configs])
    problems = check(merged, live, stage, ready, stopped)
    if problems:
        die("refusing to start: " + "; ".join(problems))

    if edited_while_open(STATE, fingerprint(merged, strategy, USER_DATA), open_trades(STATE / "tradesv3.sqlite")):
        die("refusing to start: the strategy, its params or the config changed while the bot holds open trades (docs/operating-rules.md)")

    print(f"live: {name}: {strategy} on {exchange}, REAL MONEY, {live['stage']}", flush=True)
    args = ["freqtrade", "trade", "--userdir", str(USER_DATA), "--recursive-strategy-search",
            "--strategy", strategy, "--db-url", f"sqlite:///{STATE}/tradesv3.sqlite"]
    for c in configs:
        args += ["--config", str(c)]
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
