"""Redeploy commits that Vercel blocked because of who wrote them.

On a Vercel Hobby project only the owner's commits deploy. A commit from a collaborator
(for example a PR they opened from their own ChatGPT/Codex session, or the squash
commit GitHub authors in their name when that PR merges) gets the commit status
"Git author <login> must have access to the project on Vercel to create deployments."

When that status arrives for the tip of a branch, this fast-forwards the branch by one
empty commit created through the GitHub API. GitHub authors such a commit as the user
the daemon's ``gh`` is logged in as, so Vercel deploys the same tree under that user.
The ref update is not forced: if the branch has moved since the blocked commit, the
newer commit is left to deploy (or be redeployed) on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dev_agents.runtime.github import gh

RedeployScope = Literal["off", "base", "all"]

_BLOCKED_AUTHOR = re.compile(
    r"Git author (?P<author>\S+) must have access to the project on Vercel"
)


@dataclass(frozen=True)
class BlockedDeployment:
    sha: str
    author: str
    branch: str


def blocked_deployment(
    payload: dict[str, Any], *, repository: str, base_branch: str, scope: RedeployScope
) -> BlockedDeployment | None:
    """Return the branch to redeploy for a GitHub ``status`` webhook, if any.

    Only a Vercel failure naming a blocked Git author counts, and only when the commit
    is still the tip of a branch: the base branch for scope ``base``, any branch for
    scope ``all`` (the base branch wins when both match).
    """
    if scope == "off" or payload.get("context") != "Vercel" or payload.get("state") != "failure":
        return None
    if payload.get("repository", {}).get("full_name") != repository:
        return None
    match = _BLOCKED_AUTHOR.search(str(payload.get("description") or ""))
    sha = payload.get("sha")
    if match is None or not isinstance(sha, str) or not sha:
        return None
    tips = sorted(
        str(branch.get("name"))
        for branch in payload.get("branches") or []
        if isinstance(branch, dict) and (branch.get("commit") or {}).get("sha") == sha
    )
    if base_branch in tips:
        return BlockedDeployment(sha, match["author"], base_branch)
    if scope == "all" and tips:
        return BlockedDeployment(sha, match["author"], tips[0])
    return None


def redeploy(repo: Path, github: str, blocked: BlockedDeployment) -> str | None:
    """Push an empty commit as the ``gh`` user on top of the blocked one.

    Returns the new commit SHA, or None when the blocked author is the ``gh`` user
    itself (another commit from that user would be blocked the same way). Raises
    ``GitHubError`` when GitHub refuses the update, including when the branch moved.
    """
    login = gh(repo, "api", "user", "--jq", ".login")
    if login.casefold() == blocked.author.casefold():
        return None
    tree = gh(repo, "api", f"repos/{github}/git/commits/{blocked.sha}", "--jq", ".tree.sha")
    message = (
        f"chore: redeploy {blocked.sha[:7]} on Vercel\n\n"
        f"Vercel blocked the deployment of {blocked.sha} because its Git author, "
        f"{blocked.author}, has no access to the Vercel project. This empty commit "
        f"deploys the same tree as {login}."
    )
    new_sha = gh(
        repo,
        "api",
        "-X",
        "POST",
        f"repos/{github}/git/commits",
        "-f",
        f"message={message}",
        "-f",
        f"tree={tree}",
        "-f",
        f"parents[]={blocked.sha}",
        "--jq",
        ".sha",
    )
    gh(
        repo,
        "api",
        "-X",
        "PATCH",
        f"repos/{github}/git/refs/heads/{blocked.branch}",
        "-f",
        f"sha={new_sha}",
        "-F",
        "force=false",
    )
    return new_sha
