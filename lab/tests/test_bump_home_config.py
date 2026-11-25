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
  - name: trader-ml
    newName: ghcr.io/kblack0610/trader-ml
    digest: {OLD}  # sha-1111111
  - name: after
    digest: {OLD}
"""


def test_bump_sets_only_the_trader_digest():
    out = bump(KUSTOMIZATION, NEW, "sha-2222222")
    assert f"    digest: {NEW}  # sha-2222222" in out.split("\n")
    assert out.count(OLD) == 3
    assert out.replace(f"{NEW}  # sha-2222222", f"{OLD}  # sha-1111111") == KUSTOMIZATION


def test_bump_sets_the_trader_ml_digest_by_name():
    out = bump(KUSTOMIZATION, NEW, "sha-2222222", "trader-ml")
    lines = out.split("\n")
    assert lines[lines.index("  - name: trader-ml") + 2] == f"    digest: {NEW}  # sha-2222222"
    assert out.count(OLD) == 3


def test_bump_is_a_no_op_when_already_pinned():
    once = bump(KUSTOMIZATION, NEW, "sha-2222222")
    assert bump(once, NEW, "sha-2222222") == once


def test_bump_refuses_a_bad_digest_or_a_missing_entry():
    with pytest.raises(ValueError):
        bump(KUSTOMIZATION, "latest", "sha-2222222")
    with pytest.raises(ValueError):
        bump(KUSTOMIZATION.replace("name: trader\n", "name: trader-x\n"), NEW, "sha-2222222")


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


PULL = "/repos/kblack0610/home-config/pulls/645"
SQUASH = {"Do": "squash", "delete_branch_after_merge": True}


def forgejo(merge_answers, mergeable=True):
    """A fake call: each merge POST takes the next of merge_answers ("ok" merges, else a 405 message)."""
    answers, log, state = iter(merge_answers), [], {"merged": False}
    def fake(method, path, body=None, ok_404=False, ok_405=False):
        if "/commits" in path:
            return [{"sha": "abcdef0123"}]
        log.append((method, path, body))
        if method == "POST":
            answer = next(answers)
            assert ok_405
            if answer != "ok":
                return {"refused": answer}
            state["merged"] = True
            return {}
        return {"merged": state["merged"], "mergeable": mergeable, "merge_commit_sha": "abcdef0123",
                "state": "closed" if state["merged"] else "open"}
    return fake, log


def test_merge_squashes_and_checks_it_merged(monkeypatch):
    fake, log = forgejo(["ok"])
    monkeypatch.setattr(bump_home_config, "call", fake)
    bump_home_config.merge(645)
    assert log == [("GET", PULL, None), ("POST", PULL + "/merge", SQUASH), ("GET", PULL, None)]


def test_merge_retries_while_required_checks_are_pending(monkeypatch):
    # The 405 of 2026-11-25 (#1505, #1519): Forgejo read the PR mergeable while home-config's checks were pending.
    fake, log = forgejo(["not allowed to merge [reason: Not all required status checks successful]"] * 2 + ["ok"])
    sleeps = []
    monkeypatch.setattr(bump_home_config, "call", fake)
    monkeypatch.setattr(bump_home_config.time, "sleep", sleeps.append)
    bump_home_config.merge(645)
    assert [m for m, _, _ in log].count("POST") == 3
    assert sleeps == [bump_home_config.MERGE_POLL_S] * 2


def test_merge_gives_up_at_the_deadline_with_the_last_refusal(monkeypatch):
    clock = iter([0, 1, bump_home_config.MERGE_TIMEOUT_S + 1])
    fake, _ = forgejo(["checks failed"] * 2)
    monkeypatch.setattr(bump_home_config, "call", fake)
    monkeypatch.setattr(bump_home_config.time, "sleep", lambda s: None)
    monkeypatch.setattr(bump_home_config.time, "monotonic", lambda: next(clock))
    with pytest.raises(SystemExit, match="did not merge in .*last: checks failed"):
        bump_home_config.merge(645)


def test_merge_waits_without_posting_while_not_mergeable(monkeypatch):
    clock = iter([0, bump_home_config.MERGE_TIMEOUT_S + 1])
    fake, log = forgejo([], mergeable=False)
    monkeypatch.setattr(bump_home_config, "call", fake)
    monkeypatch.setattr(bump_home_config.time, "monotonic", lambda: next(clock))
    with pytest.raises(SystemExit, match="not mergeable yet"):
        bump_home_config.merge(645)
    assert [m for m, _, _ in log] == ["GET"]


def test_merge_counts_a_200_that_did_not_merge_as_a_refusal(monkeypatch):
    clock = iter([0, bump_home_config.MERGE_TIMEOUT_S + 1])
    monkeypatch.setattr(bump_home_config, "call", lambda *a, **k: {"merged": False, "mergeable": True, "state": "open"})
    monkeypatch.setattr(bump_home_config.time, "monotonic", lambda: next(clock))
    with pytest.raises(SystemExit, match="answered 200 but did not merge"):
        bump_home_config.merge(645)


def test_merge_fails_at_once_on_a_closed_pr(monkeypatch):
    monkeypatch.setattr(bump_home_config, "call", lambda *a, **k: {"mergeable": False, "state": "closed"})
    monkeypatch.setattr(bump_home_config.time, "sleep", lambda s: pytest.fail("slept"))
    with pytest.raises(SystemExit, match="closed"):
        bump_home_config.merge(645)


def test_merge_skips_the_post_when_already_merged(monkeypatch):
    calls = []
    def fake(method, path, body=None, ok_404=False):
        if "/commits" in path:
            return [{"sha": "abcdef0123"}]
        calls.append(method)
        return {"merged": True, "merge_commit_sha": "abcdef0123", "state": "closed"}
    monkeypatch.setattr(bump_home_config, "call", fake)
    bump_home_config.merge(645)
    assert calls == ["GET"]


def test_merge_fails_when_the_merge_commit_is_not_on_master(monkeypatch):
    def fake(method, path, body=None, ok_404=False):
        if "/commits" in path:
            return [{"sha": "1111111111"}]
        return {"merged": True, "merge_commit_sha": "abcdef0123", "state": "closed"}
    monkeypatch.setattr(bump_home_config, "call", fake)
    monkeypatch.setattr(bump_home_config.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="not on master"):
        bump_home_config.merge(645)


def test_merge_fails_when_a_merged_pr_has_no_merge_commit(monkeypatch):
    monkeypatch.setattr(bump_home_config, "call", lambda *a, **k: {"merged": True, "state": "closed"})
    with pytest.raises(SystemExit, match="no merge commit"):
        bump_home_config.merge(645)


def test_on_base_waits_for_the_commit_list_to_catch_up(monkeypatch):
    answers = iter([[{"sha": "1"}], [{"sha": "abc"}]])
    monkeypatch.setattr(bump_home_config, "call", lambda *a, **k: next(answers))
    monkeypatch.setattr(bump_home_config.time, "sleep", lambda s: None)
    assert bump_home_config.on_base("abc")
