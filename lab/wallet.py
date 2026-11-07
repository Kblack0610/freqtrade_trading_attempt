#!/usr/bin/env python3
"""Watch-only Trezor balances into wallet_balances, read from the account's public key alone (docs/platforms.md, Trezor).

  python -m lab.wallet            # every account in WALLET_ACCOUNTS (default /etc/trader-wallet/accounts.tsv)

WALLET_ACCOUNTS holds one account per line, `<name><TAB><public key>`, where the key is the account public key Trezor Suite
exports: an xpub (legacy, BIP44), ypub (SegWit, BIP49), zpub (native SegWit,
BIP84), or an output descriptor such as Suite's taproot `tr([fp/86'/0'/0']xpub.../<0;1>/*)`. Bitcoin mainnet
only. A missing file means no accounts, not an error.

Addresses are derived here, receive and change chains each until `[scan] gap` unused in a row, and each is
read from an Esplora API (`[esplora]` in lab/wallet.toml). Trezor's own Blockbook servers answer xpub queries
but refuse clients other than Suite, so this does not use them. The Esplora host sees every address it is
asked about; point base_url at our own instance to keep them private. Neither the key nor an address is ever
written to the database or a log line: rows carry the account name, errors carry the HTTP status only.

Nothing here can spend: a public key cannot sign. Exits 1 when any account's read failed.
"""

import datetime as dt
import os
import sys
import tomllib
from pathlib import Path

import requests
from embit.descriptor import Descriptor
from embit.networks import NETWORKS

from lab.db import connect
from lab.markets.api import Api

CONFIG = Path(os.environ.get("WALLET_CONFIG", Path(__file__).resolve().parent / "wallet.toml"))
ACCOUNTS = Path(os.environ.get("WALLET_ACCOUNTS", "/etc/trader-wallet/accounts.tsv"))
NET = NETWORKS["main"]
# The script each extended-key prefix stands for, as Suite exports them.
PREFIX_SCRIPT = {"xpub": "pkh({})", "ypub": "sh(wpkh({}))", "zpub": "wpkh({})"}


def descriptor(key: str) -> Descriptor:
    """An account's public key or descriptor, as a descriptor over its receive and change chains."""
    key = key.strip()
    if "(" not in key:
        wrap = PREFIX_SCRIPT.get(key[:4])
        if wrap is None:
            raise ValueError(f"unsupported key prefix {key[:4]!r} (want xpub, ypub, zpub or a descriptor)")
        key = wrap.format(f"{key}/<0;1>/*")
    return Descriptor.from_string(key.split("#")[0])


def addresses(d: Descriptor, branch: int):
    i = 0
    while True:
        yield d.derive(i, branch_index=branch).address(NET)
        i += 1


def accounts(path: Path = ACCOUNTS) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, key = line.split("\t", 1)
            out.append((name.strip(), key.strip()))
    return out


class Esplora:
    def __init__(self, cfg: dict, session=None, sleep=None):
        self.api = Api("esplora", cfg, session, sleep)

    def address(self, addr: str) -> dict:
        try:
            return self.api.get(f"/address/{addr}", label="/address/<redacted>")
        except requests.HTTPError as e:
            raise RuntimeError(f"esplora: HTTP {e.response.status_code}") from None
        except requests.RequestException as e:
            raise RuntimeError(f"esplora: {type(e).__name__}") from None


def scan(d: Descriptor, esplora: Esplora, gap: int) -> dict:
    """Sum every used address on each chain, stopping a chain after `gap` unused addresses in a row."""
    confirmed = unconfirmed = used = reads = 0
    for branch in range(d.num_branches):
        idle = 0
        for addr in addresses(d, branch):
            if idle >= gap:
                break
            if reads:
                esplora.api.sleep(esplora.api.gap)
            a, reads = esplora.address(addr), reads + 1
            chain, pool = a["chain_stats"], a["mempool_stats"]
            if chain["tx_count"] + pool["tx_count"] == 0:
                idle += 1
                continue
            idle, used = 0, used + 1
            confirmed += chain["funded_txo_sum"] - chain["spent_txo_sum"]
            unconfirmed += pool["funded_txo_sum"] - pool["spent_txo_sum"]
    return {"confirmed_sat": confirmed, "unconfirmed_sat": unconfirmed, "used_addresses": used}


def read(name: str, key: str, esplora: Esplora, gap: int) -> dict:
    row = {"account": name, "coin": "btc", "confirmed_sat": None, "unconfirmed_sat": None,
           "used_addresses": None, "error": None}
    try:
        row.update(scan(descriptor(key), esplora, gap))
    except (RuntimeError, ValueError, KeyError) as e:
        row["error"] = str(e) if not isinstance(e, KeyError) else f"esplora: no field {e}"
    return row


def store(conn, rows: list[dict], now: dt.datetime) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO wallet_balances (account, read_at, coin, confirmed_sat, unconfirmed_sat, used_addresses, error)"
            " VALUES (%(account)s, %(read_at)s, %(coin)s, %(confirmed_sat)s, %(unconfirmed_sat)s, %(used_addresses)s,"
            " %(error)s)",
            [{**r, "read_at": now} for r in rows])


def main(session=None, sleep=None) -> int:
    found = accounts()
    if not found:
        print(f"wallet: no accounts in {ACCOUNTS}")
        return 0
    cfg = tomllib.loads(CONFIG.read_text())
    esplora, gap = Esplora(cfg["esplora"], session, sleep), int(cfg["scan"]["gap"])
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    rows = [read(name, key, esplora, gap) for name, key in found]
    with connect(autocommit=True) as conn:
        store(conn, rows, now)
    for r in rows:
        print(f"wallet: {r['account']}: {r['confirmed_sat']} sat confirmed, {r['used_addresses']} used, error={r['error']}")
    return 1 if any(r["error"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
