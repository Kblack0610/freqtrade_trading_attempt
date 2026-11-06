import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "bump_home_config", pathlib.Path(__file__).resolve().parents[2] / "ci" / "bump_home_config.py")
bump_home_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_home_config)
bump = bump_home_config.bump

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
KUSTOMIZATION = f"""resources:
  - deployment.yaml

images:
  - name: other
    newName: ghcr.io/x/other
    digest: {OLD}
  # The lab image.
  - name: trader
    newName: ghcr.io/kblack0610/trader
    digest: {OLD}  # sha-1111111
  - name: after
    digest: {OLD}
"""


def test_bump_sets_only_the_trader_digest():
    out = bump(KUSTOMIZATION, NEW, "sha-2222222")
    assert f"    digest: {NEW}  # sha-2222222" in out.split("\n")
    assert out.count(OLD) == 2
    assert out.replace(f"{NEW}  # sha-2222222", f"{OLD}  # sha-1111111") == KUSTOMIZATION


def test_bump_is_a_no_op_when_already_pinned():
    once = bump(KUSTOMIZATION, NEW, "sha-2222222")
    assert bump(once, NEW, "sha-2222222") == once


def test_bump_refuses_a_bad_digest_or_a_missing_entry():
    with pytest.raises(ValueError):
        bump(KUSTOMIZATION, "latest", "sha-2222222")
    with pytest.raises(ValueError):
        bump(KUSTOMIZATION.replace("name: trader", "name: trader-ml"), NEW, "sha-2222222")
