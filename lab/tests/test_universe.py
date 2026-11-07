import json

from lab import universe

LIST = ["BTC/USDT", "ETH/USDT"]


def make(tmp_path, entry=None):
    ex = tmp_path / "exchanges"
    ex.mkdir()
    (ex / "okx.json").write_text(json.dumps({"exchange": {"pair_whitelist": LIST}}))
    if entry:
        universe.save(tmp_path, "okx", entry)
    return tmp_path


def entry(pairs=LIST, source=universe.CURRENT_LISTING):
    return {"source": source, "as_of": "2026-11-07", "listed_by": "2022-01-01", "min_volume": 2e6,
            "pairs": {p: {"volume": 1, "volume_then": 1} for p in pairs}}


def test_no_record_means_current_listing_with_no_provenance(tmp_path):
    assert universe.describe_all(make(tmp_path), ["okx"]) == {"okx": {"source": "current-listing", "recorded": False, "pairs": 2}}


def test_a_matching_record_is_carried_onto_the_run(tmp_path):
    got = universe.describe_all(make(tmp_path, entry()), ["okx"])["okx"]
    assert got == {"source": "current-listing", "recorded": True, "pairs": 2, "as_of": "2026-11-07",
                   "listed_by": "2022-01-01", "min_volume": 2e6}


def test_a_whitelist_edited_since_its_record_is_not_trusted(tmp_path):
    got = universe.describe_all(make(tmp_path, entry(pairs=["BTC/USDT", "SOL/USDT"], source=universe.POINT_IN_TIME)), ["okx"])
    assert got["okx"] == {"source": "current-listing", "recorded": False, "pairs": 2}


def test_save_keeps_the_other_venues(tmp_path):
    universe.save(tmp_path, "okx", entry())
    universe.save(tmp_path, "kucoin", entry())
    assert sorted(universe.load(tmp_path)) == ["kucoin", "okx"]


def test_the_committed_exchange_files_all_resolve(tmp_path):
    from pathlib import Path
    user_data = Path(__file__).resolve().parents[2] / "user_data"
    names = sorted(p.stem for p in (user_data / "exchanges").glob("*.json"))
    got = universe.describe_all(user_data, names)
    assert sorted(got) == names and all(u["pairs"] > 0 for u in got.values())
