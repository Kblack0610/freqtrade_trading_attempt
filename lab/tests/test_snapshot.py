import pytest
from pyarrow import fs as pafs

from lab import snapshot


@pytest.fixture
def store(tmp_path):
    (tmp_path / "bucket").mkdir()
    return pafs.LocalFileSystem(), str(tmp_path / "bucket" / "candles")


def candles(data, files):
    for name, body in files.items():
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_bytes(body)


def test_publish_then_fetch_copies_the_candles_byte_for_byte(store, tmp_path):
    fs, root = store
    src, dst = tmp_path / "pvc", tmp_path / "pod"
    candles(src, {"okx/BTC_USDT-5m.feather": b"a" * 100, "okx/ETH_USDT-5m.feather": b"b", "kucoin/BTC_USDT-1m.feather": b"c"})
    out = snapshot.publish(fs, root, src)
    assert out["files"] == 3 and out["uploaded"] == 3
    got = snapshot.fetch(fs, ["okx"], root, dst)
    assert got["digest"] == out["digest"] and got["files"] == 2
    assert snapshot.local_manifest(dst) == {k: v for k, v in snapshot.local_manifest(src).items() if k.startswith("okx/")}
    assert not list(dst.rglob("*.part"))


def test_a_publish_uploads_only_changed_files_and_drops_blobs_no_kept_manifest_names(store, tmp_path):
    fs, root = store
    src = tmp_path / "pvc"
    candles(src, {"okx/A-5m.feather": b"1", "okx/B-5m.feather": b"2"})
    snapshot.publish(fs, root, src, keep=2)
    candles(src, {"okx/A-5m.feather": b"1+"})
    assert snapshot.publish(fs, root, src, keep=2)["uploaded"] == 1
    candles(src, {"okx/A-5m.feather": b"1++"})
    third = snapshot.publish(fs, root, src, keep=2)
    assert third["deleted"] == 1, "the first A is in neither kept manifest"
    assert len(snapshot._names(fs, f"{root}/manifests")) == 2
    dst = tmp_path / "pod"
    snapshot.fetch(fs, [], root, dst)
    assert (dst / "okx/A-5m.feather").read_bytes() == b"1++"


def test_an_empty_data_dir_publishes_nothing(store, tmp_path):
    fs, root = store
    with pytest.raises(SystemExit):
        snapshot.publish(fs, root, tmp_path / "missing")
