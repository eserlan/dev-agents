import json
import subprocess
from pathlib import Path

import pytest

from dev_agents.runtime import AgentResult
from dev_agents.workflows import committing_agent, sweep
from dev_agents.workflows.sweep import PersonaError, parse_persona, sweep_gate, unescape_newlines

PERSONA_FILE = """---
title: "⚡ Bolt"
journal: .agents/journal/bolt.md
max_files: 2
max_changed_lines: 10
---
Find one measurable performance improvement on a hot path.
"""
BOLT = parse_persona("bolt", PERSONA_FILE)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()


def make_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "staging")
    git(origin, "config", "user.email", "test@example.test")
    git(origin, "config", "user.name", "Test")
    (origin / ".agents/sweeps").mkdir(parents=True)
    (origin / ".agents/sweeps/bolt.md").write_text(PERSONA_FILE)
    (origin / "src").mkdir()
    (origin / "src/a.ts").write_text("export const a = [1, 2].map((x) => x).filter(Boolean);\n")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "base")
    git(origin, "config", "receive.denyCurrentBranch", "ignore")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    return repo


def test_parse_persona_reads_front_matter_and_instructions() -> None:
    assert BOLT.title == "⚡ Bolt"
    assert BOLT.journal == ".agents/journal/bolt.md"
    assert (BOLT.max_files, BOLT.max_changed_lines) == (2, 10)
    assert BOLT.instructions == "Find one measurable performance improvement on a hot path."
    assert BOLT.branch_prefix == "bolt/"
    defaults = parse_persona("palette", '---\ntitle: "🎨 Palette"\n---\nBody')
    assert (defaults.journal, defaults.max_files, defaults.max_changed_lines) == (".agents/journal/palette.md", 6, 150)
    with pytest.raises(PersonaError):
        parse_persona("x", "no front matter")
    with pytest.raises(PersonaError):
        parse_persona("x", "---\njournal: j.md\n---\nmissing title")


def test_gate_ignores_the_journal_and_enforces_limits() -> None:
    journal = f"40\t0\t{BOLT.journal}"
    assert sweep_gate(f"3\t2\tsrc/a.ts\n{journal}", BOLT) is None
    assert sweep_gate(journal, BOLT) == "the commit only touched the journal"
    assert sweep_gate("1\t0\ta\n1\t0\tb\n1\t0\tc", BOLT) == "changed 3 files (limit 2)"
    assert sweep_gate("9\t9\tsrc/a.ts", BOLT) == "changed 18 lines (limit 10)"
    assert sweep_gate("-\t-\tlogo.png", BOLT) is None


def test_escaped_newlines_become_line_breaks_outside_code() -> None:
    body = "💡 What: x.\\n🎯 Why: y.\n\nUses `split(\"\\n\")` here."
    assert unescape_newlines(body) == "💡 What: x.\n🎯 Why: y.\n\nUses `split(\"\\n\")` here."


def test_persona_is_read_from_the_fetched_base_not_the_local_checkout(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / ".agents/sweeps/bolt.md").write_text('---\ntitle: "local edit"\n---\nignored')
    assert sweep.load_persona(repo, "staging", ".agents/sweeps", "bolt").title == "⚡ Bolt"
    with pytest.raises(PersonaError, match="not found"):
        sweep.load_persona(repo, "staging", ".agents/sweeps", "palette")
    with pytest.raises(PersonaError, match="invalid persona name"):
        sweep.load_persona(repo, "staging", ".agents/sweeps", "../bolt")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path)
    created: list[tuple[str, ...]] = []
    prompts: list[str] = []
    monkeypatch.setattr(sweep, "open_pull_request_with_prefix", lambda _repo, _prefix: None)
    monkeypatch.setattr(sweep, "gh_json", lambda *_args: [
        {"title": "⚡ Bolt: Earlier Jules change", "headRefName": "bolt/old-1", "state": "MERGED"},
        {"title": "🎨 Palette: unrelated", "headRefName": "palette/x", "state": "OPEN"},
    ])
    monkeypatch.setattr(sweep, "_gh", lambda _repo, *args, **_kwargs: created.append(args) or "https://example.test/pr/9")
    installs: list[Path] = []
    monkeypatch.setattr(sweep, "install_dependencies", lambda worktree: installs.append(worktree) or True)
    return repo, created, prompts, installs


def run(repo: Path, monkeypatch: pytest.MonkeyPatch, prompts: list[str], behaviour) -> sweep.SweepRunResult:
    def agent(_name: str, prompt: str, **kwargs) -> AgentResult:
        prompts.append(prompt)
        behaviour(kwargs["cwd"], prompt)
        return AgentResult(0, False)

    monkeypatch.setattr(committing_agent, "run_agent", agent)
    return sweep._run_sweep_impl(repo, BOLT, dry_run=False, log_path=repo.parent / "log")


def commit(worktree: Path, message: str, *, lines: int = 1, journal: bool = True) -> None:
    (worktree / "src/a.ts").write_text("".join(f"export const a{i} = {i};\n" for i in range(lines)))
    if journal:
        (worktree / ".agents/journal").mkdir(parents=True, exist_ok=True)
        (worktree / BOLT.journal).write_text("## 2026-11-02 - lesson\n" * 30)
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", message)


def test_a_focused_change_becomes_a_pr_against_the_base(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, created, prompts, installs = sandbox
    result = run(repo, monkeypatch, prompts, lambda wt, _p: commit(wt, "Single-pass filter in a.ts\n\nWhat: one loop.\\nWhy: hot path."))

    assert result.succeeded and result.pull_request_url == "https://example.test/pr/9"
    assert result.branch and result.branch.startswith("bolt/sweep-")
    args = created[0]
    assert args[args.index("--base") + 1] == "staging"
    assert args[args.index("--title") + 1] == "⚡ Bolt: Single-pass filter in a.ts"
    body = args[args.index("--body") + 1]
    assert body.startswith("What: one loop.\nWhy: hot path.") and "Scheduled `bolt` sweep" in body
    assert "[merged] ⚡ Bolt: Earlier Jules change" in prompts[0] and "Palette" not in prompts[0]
    assert git(repo, "ls-remote", "origin", result.branch)
    # Dependencies are installed in the worktree before the agent validates its change.
    assert len(installs) == 1
    # The local branch and worktree are cleaned up once pushed.
    assert git(repo, "branch", "--list", result.branch) == ""


def test_nothing_worth_changing_opens_no_pr(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, created, prompts, _installs = sandbox

    def noop(_worktree: Path, prompt: str) -> None:
        marker = prompt.split("write a one-line reason to `")[1].split("`")[0]
        Path(marker).write_text("every hot path is already a single pass\n")

    result = run(repo, monkeypatch, prompts, noop)

    assert result.succeeded and result.noop and not created and result.branch is None
    assert result.reason == "nothing worth changing: every hot path is already a single pass"
    assert len(prompts) == 1


def test_an_oversized_change_is_not_pushed(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, created, prompts, _installs = sandbox
    result = run(repo, monkeypatch, prompts, lambda wt, _p: commit(wt, "Big", lines=20, journal=False))

    assert not result.succeeded and not created
    assert result.reason == "changed 21 lines (limit 10)"
    assert git(repo, "ls-remote", "origin", "refs/heads/bolt/*") == ""


def test_a_sweep_waits_while_the_persona_has_an_open_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep, "open_pull_request_with_prefix", lambda _repo, prefix: f"https://example.test/{prefix}")
    monkeypatch.setattr(committing_agent, "run_agent", lambda *_a, **_k: pytest.fail("agent must not run"))

    result = sweep._run_sweep_impl(tmp_path, BOLT, dry_run=False)

    assert result.succeeded and result.reason == "waiting for open bolt PR https://example.test/bolt/"


def test_cli_dry_run_loads_the_persona_without_mutating(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dev_agents.cli import main

    repo = make_repo(tmp_path)
    config = tmp_path / "projects.yaml"
    config.write_text(f"projects:\n  demo:\n    repo: {repo}\n")

    assert main(["sweep", "run", "demo", "--persona", "bolt", "--config", str(config)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dryRun"] is True and output["persona"] == "bolt" and output["branch"].startswith("bolt/sweep-")
    assert git(repo, "branch", "--list", "bolt/*") == ""


FAILED_LOG_LINES = [
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:40:00.0Z   1) [chromium] › tests/a.spec.ts:3:1 › A › first attempt noise",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:43.59Z   2 failed",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:43.59Z     [chromium] › tests/a.spec.ts:3:1 › A › opens the codex",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:43.59Z     [chromium] › tests/b.spec.ts:9:1 › B › switches vault",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:43.59Z   1 flaky",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:43.59Z     [chromium] › tests/c.spec.ts:1:1 › C › flaky one",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:43.59Z   \x1b[32m40 passed\x1b[39m (41.7m)",
    "Run E2E Tests\tUNKNOWN STEP\t2026-11-02T03:53:44.06Z ##[notice]  2 failed",
    "Run E2E Tests\tUNKNOWN STEP\t    [chromium] › tests/a.spec.ts:3:1 › A › opens the codex",
]
FAILED_LOG = "\n".join(FAILED_LOG_LINES)


def test_failed_log_summary_keeps_totals_and_failed_titles_only() -> None:
    assert sweep.summarize_failed_log(FAILED_LOG).splitlines() == [
        "2 failed",
        "  - [chromium] › tests/a.spec.ts:3:1 › A › opens the codex",
        "  - [chromium] › tests/b.spec.ts:9:1 › B › switches vault",
        "1 flaky",
        "40 passed (41.7m)",
    ]
    assert sweep.summarize_failed_log("just\nsome\nlines") == "just\nsome\nlines"


VIGIL_FILE = """---
title: "🛡️ Vigil"
timeout_minutes: 75
failing_workflow: daily-e2e.yml
---
Fix one root cause of the failing E2E tests.
"""


@pytest.mark.parametrize(("runs", "reason"), [
    ([], "no daily-e2e.yml runs on staging"),
    ([{"status": "in_progress", "conclusion": None}], "latest daily-e2e.yml run on staging has not finished"),
    ([{"status": "completed", "conclusion": "success"}], "latest daily-e2e.yml run on staging concluded success"),
    ([{"status": "completed", "conclusion": "cancelled"}], "latest daily-e2e.yml run on staging concluded cancelled"),
])
def test_a_green_or_unfinished_workflow_skips_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs, reason) -> None:
    vigil = parse_persona("vigil", VIGIL_FILE)
    assert (vigil.timeout_minutes, vigil.failing_workflow) == (75, "daily-e2e.yml")
    monkeypatch.setattr(sweep, "open_pull_request_with_prefix", lambda _repo, _prefix: None)
    monkeypatch.setattr(sweep, "gh_json", lambda *_args: runs)
    monkeypatch.setattr(committing_agent, "run_agent", lambda *_a, **_k: pytest.fail("agent must not run"))

    result = sweep._run_sweep_impl(tmp_path, vigil, dry_run=False)

    assert result.succeeded and result.noop and result.reason == f"nothing to fix: {reason}"


def test_a_failed_workflow_hands_its_failures_and_timeout_to_the_agent(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, created, prompts, _installs = sandbox
    vigil = parse_persona("vigil", VIGIL_FILE)
    run = {"databaseId": 77, "status": "completed", "conclusion": "failure", "url": "https://example.test/run/77", "createdAt": "2026-11-02T03:11:24Z"}
    monkeypatch.setattr(sweep, "gh_json", lambda _repo, *args: [run] if args[:2] == ("run", "list") else [])
    monkeypatch.setattr(sweep, "_gh", lambda _repo, *args, **_kwargs: FAILED_LOG if args[:2] == ("run", "view") else created.append(args) or "https://example.test/pr/10")
    budgets: list[float] = []

    def agent(_name: str, prompt: str, **kwargs) -> AgentResult:
        prompts.append(prompt)
        budgets.append(kwargs["timeout_seconds"])
        commit(kwargs["cwd"], "Disambiguate the Open Codex link\n\nFixes 2 failing specs.", journal=False)
        return AgentResult(0, False)

    monkeypatch.setattr(committing_agent, "run_agent", agent)
    result = sweep._run_sweep_impl(repo, vigil, dry_run=False, log_path=repo.parent / "log", timeout_seconds=60)

    assert result.succeeded and result.pull_request_url == "https://example.test/pr/10"
    assert "https://example.test/run/77" in prompts[0] and "gh run view 77 --log-failed" in prompts[0]
    assert "  - [chromium] › tests/b.spec.ts:9:1 › B › switches vault" in prompts[0]
    assert budgets[0] > 60  # the persona's 75 minutes, not the configured minute
