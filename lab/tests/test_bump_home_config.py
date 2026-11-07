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


def test_superseded_prs_close_with_a_note_and_no_issue_comment(monkeypatch):
    calls = []
    monkeypatch.setattr(bump_home_config, "call", lambda method, path, body=None, ok_404=False: calls.append((method, path, body)))
    open_prs = [{"number": 644, "head": {"ref": "trader-sha-1111111"}, "body": "Pins it."},
                {"number": 645, "head": {"ref": "trader-sha-2222222"}, "body": "Pins it."},
                {"number": 9, "head": {"ref": "someone-else"}, "body": None}]
    bump_home_config.close_superseded(open_prs, "trader-sha-2222222", 645, "sha-2222222")
    assert calls == [("PATCH", "/repos/kblack0610/home-config/pulls/644",
                      {"state": "closed", "body": "Pins it.\n\nSuperseded by #645 (sha-2222222)."}),
                     ("DELETE", "/repos/kblack0610/home-config/branches/trader-sha-1111111", None)]


def test_merge_squashes_and_checks_it_merged(monkeypatch):
    calls = []
    def fake(method, path, body=None, ok_404=False):
        calls.append((method, path, body))
        return {"merged": merged, "merge_commit_sha": "abcdef0123", "state": "closed"}
    monkeypatch.setattr(bump_home_config, "call", fake)
    merged = True
    bump_home_config.merge(645)
    assert calls == [("POST", "/repos/kblack0610/home-config/pulls/645/merge",
                      {"Do": "squash", "delete_branch_after_merge": True}),
                     ("GET", "/repos/kblack0610/home-config/pulls/645", None)]
    merged = False
    with pytest.raises(SystemExit):
        bump_home_config.merge(645)
