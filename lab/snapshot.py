#!/usr/bin/env python3
"""Candle snapshot: the backfill PVC's candles as a read-only copy in the object store, so lab pods on any node
read the same candles without mounting the PVC (which pins them to its node).

publish (on the PVC's node, after a backfill): uploads each candle file under LAB_RESULTS/data whose content is not
stored yet, by its sha256 (<root>/blobs/<sha>), then a manifest of {"<exchange>/<file>": sha}
(<root>/manifests/<digest>.json, digest the sha256 of that manifest) and <root>/latest naming it. A file that did not
change is not uploaded again, and a reader always sees one whole manifest. It keeps the newest SNAPSHOT_KEEP
manifests and deletes the blobs none of them names.

fetch [exchange ...] (in each lab pod, before its job): downloads the latest manifest's files for those exchanges
(all, with none named) into LAB_RESULTS/data and prints the manifest digest.

The cache keys are unchanged: walk-forward and analysis still digest the candles they read (analyze.candle_digests),
and the copy is byte for byte, so a fold run from the snapshot keys the same as one run on the PVC.
"""

import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

from pyarrow import fs as pafs

from lab.util import s3_from_env

RESULTS = Path(os.environ.get("LAB_RESULTS", "/lab"))
ROOT = os.environ.get("SNAPSHOT_ROOT", "trader-markets/candles")  # bucket/prefix
KEEP = int(os.environ.get("SNAPSHOT_KEEP", "2"))  # the one a running job may still read, and the new one
CHUNK = 8 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def local_manifest(data: Path) -> dict[str, str]:
    """{"<exchange>/<file>": sha256} for every candle file under data."""
    return {f"{p.parent.name}/{p.name}": sha256_file(p) for p in sorted(data.glob("*/*.feather")) if p.is_file()}


def digest(manifest: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _names(fs: pafs.FileSystem, base: str) -> list[pafs.FileInfo]:
    return [i for i in fs.get_file_info(pafs.FileSelector(base, allow_not_found=True))
            if i.type == pafs.FileType.File]


def _write(fs: pafs.FileSystem, path: str, data: bytes) -> None:
    with fs.open_output_stream(path) as out:
        out.write(data)


def _read(fs: pafs.FileSystem, path: str) -> bytes:
    with fs.open_input_stream(path) as f:
        return f.read()


def publish(fs: pafs.FileSystem, root: str = ROOT, data: Path | None = None, keep: int = KEEP) -> dict:
    data = data or RESULTS / "data"
    manifest = local_manifest(data)
    if not manifest:
        raise SystemExit(f"snapshot: no candle files under {data}; is the PVC mounted? Not publishing.")
    if fs.type_name == "local":  # an object store has no directories to make
        for sub in ("blobs", "manifests"):
            fs.create_dir(f"{root}/{sub}", recursive=True)
    stored = {i.base_name for i in _names(fs, f"{root}/blobs")}
    uploaded = 0
    for name, sha in manifest.items():
        if sha in stored:
            continue
        with (data / name).open("rb") as src, fs.open_output_stream(f"{root}/blobs/{sha}") as out:
            while b := src.read(CHUNK):
                out.write(b)
        stored.add(sha)
        uploaded += 1
    d = digest(manifest)
    _write(fs, f"{root}/manifests/{d}.json", json.dumps(
        {"files": manifest, "published_at": dt.datetime.now(dt.timezone.utc).isoformat()}, sort_keys=True).encode())
    _write(fs, f"{root}/latest", d.encode())
    # newest first by publish time, the one just written always kept
    kept = sorted(_names(fs, f"{root}/manifests"), key=lambda i: (i.base_name != f"{d}.json", -(i.mtime_ns or 0)))[:keep]
    for i in _names(fs, f"{root}/manifests"):
        if i.path not in {k.path for k in kept}:
            fs.delete_file(i.path)
    live = {sha for i in kept for sha in json.loads(_read(fs, i.path))["files"].values()}
    gone = [i for i in _names(fs, f"{root}/blobs") if i.base_name not in live]
    for i in gone:
        fs.delete_file(i.path)
    return {"digest": d, "files": len(manifest), "uploaded": uploaded, "deleted": len(gone),
            "bytes": sum((data / n).stat().st_size for n in manifest)}


def fetch(fs: pafs.FileSystem, exchanges: list[str], root: str = ROOT, data: Path | None = None) -> dict:
    data = data or RESULTS / "data"
    d = _read(fs, f"{root}/latest").decode().strip()
    files = json.loads(_read(fs, f"{root}/manifests/{d}.json"))["files"]
    want = {n: sha for n, sha in files.items() if not exchanges or n.split("/", 1)[0] in exchanges}
    for name, sha in want.items():
        dest = data / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and sha256_file(dest) == sha:
            continue
        part = dest.with_name(dest.name + ".part")
        with fs.open_input_stream(f"{root}/blobs/{sha}") as src, part.open("wb") as out:
            while b := src.read(CHUNK):
                out.write(b)
        part.replace(dest)
    return {"digest": d, "files": len(want)}


def main(argv: list[str] | None = None, fs: pafs.FileSystem | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("publish", "fetch"):
        print("usage: snapshot.py publish | fetch [exchange ...]", file=sys.stderr)
        return 2
    fs = fs or s3_from_env()
    out = publish(fs) if argv[0] == "publish" else fetch(fs, argv[1:])
    print(f"snapshot: {argv[0]}: {json.dumps(out)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
