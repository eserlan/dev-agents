import subprocess
from pathlib import Path
from threading import Event, Thread

from dev_agents.runtime import isolated_worktree, repo_git_lock, repo_worktree_semaphore


def test_repo_git_lock_same_path_returns_same_instance(tmp_path: Path) -> None:
    """PR-fixer, issue-fixer, and release-comms all fetch against the same shared
    checkout; they must contend on the same lock instance for a given repo, or the
    lock does nothing to prevent the ref compare-and-swap race between them."""
    repo = tmp_path / "repo"
    repo.mkdir()

    first = repo_git_lock(repo)
    second = repo_git_lock(repo)
    assert first is second

    # A relative/differently-spelled path to the same directory must still resolve
    # to the same lock.
    third = repo_git_lock(Path(str(repo) + "/."))
    assert first is third


def test_repo_git_lock_different_paths_return_different_instances(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    assert repo_git_lock(repo_a) is not repo_git_lock(repo_b)


def test_repo_worktree_semaphore_same_path_returns_same_instance(tmp_path: Path) -> None:
    """PR-fixer and issue-fixer must share one semaphore per repo, or a cap of N
    each becomes an effective cap of 2N running concurrently against one project."""
    repo = tmp_path / "repo"
    repo.mkdir()

    assert repo_worktree_semaphore(repo, 2) is repo_worktree_semaphore(repo, 2)


def _init_origin_with_branch(origin: Path, branch: str) -> None:
    for command in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=origin, check=True, capture_output=True)
    (origin / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", branch], cwd=origin, check=True, capture_output=True)
    (origin / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=origin, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=origin, check=True, capture_output=True)


def test_isolated_worktree_blocks_beyond_max_concurrent(tmp_path: Path) -> None:
    """The whole point of the cap: a second worktree for the same repo must wait
    for an active one to close, not run alongside it and oversubscribe CPU."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_origin_with_branch(origin, "feature")
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(origin), str(checkout)], check=True, capture_output=True
    )
    worktree_root = tmp_path / "worktrees"

    entered_second = Event()

    def hold_second() -> None:
        with isolated_worktree(checkout, worktree_root, "feature", "main", max_concurrent=1):
            entered_second.set()

    with isolated_worktree(checkout, worktree_root, "feature", "main", max_concurrent=1):
        thread = Thread(target=hold_second, daemon=True)
        thread.start()
        # The second worktree must NOT be able to enter while the first is open.
        assert not entered_second.wait(timeout=0.5)

    # Released now that the first has exited -- the second proceeds.
    assert entered_second.wait(timeout=5)
    thread.join(timeout=5)


def test_isolated_worktree_sees_new_commits_pushed_between_calls(tmp_path: Path) -> None:
    """A bare `git fetch origin <branch>` only populates FETCH_HEAD; it does not
    update refs/remotes/origin/<branch> unless that ref is already covered by the
    repo's configured remote.origin.fetch refspec -- which a single-branch clone
    (--single-branch, as this project's real checkouts use) deliberately does not
    do for any branch but the default one. Confirmed live on a real PR: three
    separate fresh fetches in a row all resolved origin/{branch} to a commit two
    pushes behind the actual remote tip. Each isolated_worktree call must see
    whatever is actually on the remote right now, not a stale cached ref from an
    earlier call in the same checkout.

    Uses a --single-branch clone deliberately: a full clone's wildcard fetch
    refspec masks this bug entirely (this test would pass even with the buggy
    bare-fetch form), which is exactly why it slipped through unnoticed until it
    showed up live."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_origin_with_branch(origin, "feature")
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--single-branch", "--branch", "main", str(origin), str(checkout)],
        check=True,
        capture_output=True,
    )
    worktree_root = tmp_path / "worktrees"

    with isolated_worktree(checkout, worktree_root, "feature", "main", max_concurrent=1) as (
        worktree,
        _conflicts,
    ):
        first_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
        ).stdout.strip()

    # A second, independent push to the same branch -- no rewrite, just a
    # fast-forward -- after the checkout already has a cached origin/feature ref
    # from the call above.
    subprocess.run(["git", "checkout", "feature"], cwd=origin, check=True, capture_output=True)
    (origin / "feature.txt").write_text("feature v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feature v2"], cwd=origin, check=True, capture_output=True
    )
    subprocess.run(["git", "checkout", "main"], cwd=origin, check=True, capture_output=True)
    true_tip = subprocess.run(
        ["git", "rev-parse", "feature"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()

    with isolated_worktree(checkout, worktree_root, "feature", "main", max_concurrent=1) as (
        worktree,
        _conflicts,
    ):
        second_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
        ).stdout.strip()

    assert second_head != first_head
    assert second_head == true_tip
