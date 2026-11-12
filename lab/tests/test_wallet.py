"""Watch-only wallet reads: address derivation against the BIP44/49/84/86 test vectors (the public "abandon ...
about" mnemonic), the gap-limit scan against a fake Esplora, and that no key or address reaches a row or error."""

import datetime as dt

import pytest
import requests
from markets_fakes import Resp, Session

from lab import wallet

# Account 0 public keys of the BIP44, BIP49 and BIP84 test vectors.
XPUB = "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj"
YPUB = "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP"
ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
TR_XPUB = "xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ"
CFG = {"base_url": "https://esplora.test", "page_gap_s": 0, "retries": 0}


def first(key, branch=0):
    return next(wallet.addresses(wallet.descriptor(key), branch))


def test_each_suite_key_type_derives_its_bip_test_vector_addresses():
    assert first(XPUB) == "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"
    assert first(YPUB) == "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf"
    assert first(ZPUB) == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    assert first(ZPUB, branch=1) == "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
    tr = f"tr([73c5da0a/86'/0'/0']{TR_XPUB}/<0;1>/*)#checksum"  # Suite's taproot export shape
    assert first(tr) == "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"


def test_an_unknown_prefix_is_refused():
    with pytest.raises(ValueError, match="unsupported key prefix"):
        wallet.descriptor("tpubD6NzVbkrYhZ4X")


def stats(funded=0, spent=0, txs=0, pool_funded=0, pool_txs=0):
    return {"chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": spent, "tx_count": txs},
            "mempool_stats": {"funded_txo_sum": pool_funded, "spent_txo_sum": 0, "tx_count": pool_txs}}


def test_each_chain_is_scanned_to_the_gap_and_used_addresses_are_summed():
    used, idle = stats(funded=150_000, spent=50_000, txs=3), stats()
    s, slept = Session((200, used), (200, idle), (200, stats(pool_funded=7, pool_txs=1)), (200, idle), (200, idle),
                       (200, idle), (200, idle)), []
    out = wallet.scan(wallet.descriptor(ZPUB), wallet.Esplora({**CFG, "page_gap_s": 1}, s, slept.append), gap=2)
    assert out == {"confirmed_sat": 100_000, "unconfirmed_sat": 7, "used_addresses": 2}
    assert len(s.calls) == 7 and slept == [1.0] * 6, "Api's gap, once between requests"  # receive: used, idle, used, idle, idle; change: idle, idle
    assert s.calls[0][0] == "https://esplora.test/address/bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


class HttpErr(Resp):
    def raise_for_status(self):
        e = requests.HTTPError("404 for url https://esplora.test/address/bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")
        e.response = self
        raise e


def test_a_failed_read_names_the_status_and_never_the_address():
    s = Session()
    s.get = lambda url, params, timeout: HttpErr(404)
    row = wallet.read("cold", ZPUB, wallet.Esplora(CFG, s, lambda _: None), gap=20)
    assert row["error"] == "esplora: HTTP 404" and row["confirmed_sat"] is None
    assert "bc1" not in row["error"] and ZPUB not in str(row)


def test_accounts_file_skips_comments_and_a_missing_file_is_no_accounts(tmp_path):
    f = tmp_path / "accounts.tsv"
    f.write_text(f"# name\tkey\ncold\t{ZPUB}\n\nsavings\t{XPUB}\n")
    assert wallet.accounts(f) == [("cold", ZPUB), ("savings", XPUB)]
    assert wallet.accounts(tmp_path / "none.tsv") == []


def test_rows_land_by_account_name_only(conn):
    now = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
    rows = [{"account": "cold", "coin": "btc", "confirmed_sat": 100_000, "unconfirmed_sat": 0, "used_addresses": 2,
             "error": None}]
    wallet.store(conn, rows, now)
    assert conn.execute("SELECT account, read_at, confirmed_sat, used_addresses FROM wallet_balances").fetchall() == [
        ("cold", now, 100_000, 2)]
