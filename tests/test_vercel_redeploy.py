from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dev_agents.runtime.github import GitHubError
from dev_agents.vercel_redeploy import BlockedDeployment, blocked_deployment, redeploy

SHA = "cf55843" + "0" * 33
BLOCKED = (
    "Git author auricle74 must have access to the project on Vercel to create deployments."
)


def _status(
    *,
    description: str = BLOCKED,
    state: str = "failure",
    context: str = "Vercel",
    branches: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "sha": SHA,
        "state": state,
        "context": context,
        "description": description,
        "repository": {"full_name": "owner/repo"},
        "branches": [
            {"name": name, "commit": {"sha": sha}}
            for name, sha in (branches if branches is not None else [("main", SHA)])
        ],
    }


def _blocked(payload: dict[str, Any], scope: str = "all") -> BlockedDeployment | None:
    return blocked_deployment(
        payload, repository="owner/repo", base_branch="main", scope=scope  # type: ignore[arg-type]
    )


def test_blocked_author_on_base_branch_tip_is_redeployed() -> None:
    assert _blocked(_status(), scope="base") == BlockedDeployment(SHA, "auricle74", "main")


def test_other_vercel_failures_and_states_are_ignored() -> None:
    assert _blocked(_status(description="Deployment was blocked")) is None
    assert _blocked(_status(description="Checks for Deployment have failed")) is None
    assert _blocked(_status(state="success")) is None
    assert _blocked(_status(context="ci")) is None
    other_repo = _status()
    other_repo["repository"] = {"full_name": "someone/else"}
    assert _blocked(other_repo) is None


def test_commit_that_is_no_longer_a_branch_tip_is_ignored() -> None:
    assert _blocked(_status(branches=[("main", "f" * 40)])) is None


def test_pr_branches_need_scope_all() -> None:
    payload = _status(branches=[("fix/thing", SHA)])
    assert _blocked(payload, scope="base") is None
    assert _blocked(payload, scope="all") == BlockedDeployment(SHA, "auricle74", "fix/thing")
    assert _blocked(_status(), scope="off") is None


class FakeGh:
    def __init__(self, login: str = "eserlan", fail_ref: bool = False) -> None:
        self.login = login
        self.fail_ref = fail_ref
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, repo: Path, *args: str, **_: object) -> str:
        self.calls.append(args)
        if args[:2] == ("api", "user"):
            return self.login
        if args[-1] == ".tree.sha":
            return "tree123"
        if "POST" in args:
            return "new456"
        if "PATCH" in args and self.fail_ref:
            raise GitHubError("Update is not a fast forward")
        return ""


def test_redeploy_fast_forwards_branch_with_empty_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeGh()
    monkeypatch.setattr("dev_agents.vercel_redeploy.gh", fake)

    new_sha = redeploy(tmp_path, "owner/repo", BlockedDeployment(SHA, "auricle74", "fix/thing"))

    assert new_sha == "new456"
    create = next(call for call in fake.calls if "POST" in call)
    assert "tree=tree123" in create and f"parents[]={SHA}" in create
    update = fake.calls[-1]
    assert "repos/owner/repo/git/refs/heads/fix/thing" in update
    assert "sha=new456" in update and "force=false" in update


def test_redeploy_skips_when_blocked_author_is_the_gh_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeGh(login="Auricle74")
    monkeypatch.setattr("dev_agents.vercel_redeploy.gh", fake)

    assert redeploy(tmp_path, "owner/repo", BlockedDeployment(SHA, "auricle74", "main")) is None
    assert len(fake.calls) == 1


def test_redeploy_surfaces_a_moved_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dev_agents.vercel_redeploy.gh", FakeGh(fail_ref=True))

    with pytest.raises(GitHubError, match="fast forward"):
        redeploy(tmp_path, "owner/repo", BlockedDeployment(SHA, "auricle74", "main"))
