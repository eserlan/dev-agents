import json
import subprocess
from pathlib import Path

import pytest

from dev_agents.runtime import AgentResult
from dev_agents.workflows import degodify
from dev_agents.workflows.degodify import (
    DegodifyFile,
    build_decomposition_prompt,
    decomposition_gate,
    run_degodify,
    summarize_fallow_inspect,
)


def test_run_degodify_defaults_to_safe_dry_run(monkeypatch, tmp_path):
    candidate = DegodifyFile("src/large.ts", 900, 800, "Utility / Module", "WATCH")
    monkeypatch.setattr(
        "dev_agents.workflows.degodify.plan_degodify",
        lambda repo: type("Selection", (), {"candidate": candidate})(),
    )
    result = run_degodify(tmp_path)
    assert result.dry_run is True
    assert result.succeeded is True
    assert result.pull_request_url is None
    assert result.branch is not None


TARGET = "src/large.ts"
INSPECT = {
    "evidence": {
        "trace_file": {"status": "ok", "data": {"exports": [
            {"name": "themeHelpers", "referenced_by": [{"from_file": "src/Form.svelte"}]},
            {"name": "unusedThing", "referenced_by": []},
        ]}},
        "complexity": {"status": "ok", "data": {"findings": [
            {"path": TARGET, "name": "small", "line": 3, "cyclomatic": 11, "cognitive": 12, "severity": "moderate"},
            {"path": TARGET, "name": "huge", "line": 40, "cyclomatic": 30, "cognitive": 90, "severity": "high"},
            {"path": "src/other.ts", "name": "elsewhere", "line": 1, "cyclomatic": 50, "cognitive": 99, "severity": "high"},
        ]}},
        "duplication": {"status": "ok", "data": {"clone_groups": [
            {"instances": [{"file": TARGET, "start_line": 5, "end_line": 20}, {"file": "src/b.ts", "start_line": 1, "end_line": 16}]},
        ]}},
        "dead_code": {"status": "ok", "data": {"unused_exports": [{"export_name": "unusedThing"}]}},
    }
}


def test_fallow_inspect_is_summarised_for_the_target_file() -> None:
    report = summarize_fallow_inspect(INSPECT, TARGET)

    assert report.exports == [("themeHelpers", ["src/Form.svelte"]), ("unusedThing", [])]
    assert [item["name"] for item in report.complex_functions] == ["huge", "small"]
    assert report.clones == ["large.ts:5-20, b.ts:1-16"]
    assert report.dead_code == ["unusedThing"]
    section = report.prompt_section()
    assert "- themeHelpers: Form.svelte" in section and "- unusedThing: no direct importers" in section
    assert "- huge at line 40: 30 / 90 (high)" in section


def test_decomposition_gate_requires_real_shrinkage_and_a_passing_audit() -> None:
    assert "only shrank from 1000 to 900" in decomposition_gate(1000, 900, None)
    assert decomposition_gate(1000, 400, {"verdict": "fail"}) == "fallow audit failed for the decomposition"
    assert decomposition_gate(1000, 400, {"verdict": "warn"}) is None


def test_prompt_asks_for_a_thorough_split_with_the_fallow_evidence(tmp_path: Path) -> None:
    candidate = degodify.DegodifyFile(TARGET, 3760, 3000, "Utility / Module", "CRITICAL")
    prompt = build_decomposition_prompt(candidate, "curator/x", "staging", tmp_path, summarize_fallow_inspect(INSPECT, TARGET))

    assert "One surgical extraction is not enough" in prompt
    assert "until the file is at most 500 lines" in prompt
    assert "- huge at line 40" in prompt
    assert "bunx fallow audit --base origin/staging" in prompt
    small = degodify.DegodifyFile(TARGET, 800, 700, "Utility / Module", "WATCH")
    assert "at most 400 lines" in build_decomposition_prompt(small, "b", "staging", tmp_path)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()


@pytest.mark.parametrize("recovery", ["immediate", "noop", "dirty", "fallback", "exhausted", "timeout"])
@pytest.mark.parametrize("kept_lines, expect_pr", [(200, True), (900, False)])
def test_execute_opens_a_pr_only_for_a_substantial_decomposition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kept_lines: int, expect_pr: bool, recovery: str) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "staging")
    git(origin, "config", "user.email", "test@example.test")
    git(origin, "config", "user.name", "Test")
    (origin / "src").mkdir()
    (origin / "src/large.ts").write_text("".join(f"export const v{index} = {index};\n" for index in range(1000)))
    (origin / ".fallowrc.json").write_text("{}")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "base")
    git(origin, "config", "receive.denyCurrentBranch", "ignore")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "config", "user.name", "Test")
    prompts: list[str] = []
    created: list[tuple[str, ...]] = []
    budgets: dict[str, list[float]] = {}
    from types import SimpleNamespace
    clock = iter(range(0, 10000, 10))
    monkeypatch.setattr(degodify, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    def agent(_name: str, prompt: str, **kwargs) -> AgentResult:
        prompts.append(prompt)
        budgets.setdefault(_name, []).append(kwargs["timeout_seconds"])
        # The configured model and effort are Codex settings and never reach other providers.
        assert kwargs["model"] == ("decomposition-model" if _name == "codex" else None)
        assert kwargs["reasoning_effort"] == ("high" if _name == "codex" else None)
        worktree = kwargs["cwd"]
        if recovery == "exhausted" or (recovery == "fallback" and _name == "codex"):
            return AgentResult(0, False)
        if recovery == "timeout":
            return AgentResult(1, True)
        if len(prompts) == 1 and recovery in {"noop", "dirty"}:
            if recovery == "dirty":
                (worktree / "partial.txt").write_text("preserve me")
            return AgentResult(0, False)
        if recovery in {"noop", "dirty"}:
            assert "Continue in this same worktree" in prompt
        if recovery == "dirty":
            assert (worktree / "partial.txt").read_text() == "preserve me"
        lines = (worktree / TARGET).read_text().splitlines()
        (worktree / TARGET).write_text("\n".join(lines[:kept_lines]) + "\n")
        (worktree / "src/theme.ts").write_text("\n".join(lines[kept_lines:]) + "\n")
        git(worktree, "add", ".")
        git(worktree, "commit", "-m", "split")
        return AgentResult(0, False)

    monkeypatch.setattr(degodify, "run_agent", agent)
    monkeypatch.setattr(degodify, "open_degodify_pull_request", lambda _repo: None)
    monkeypatch.setattr(degodify, "_install_dependencies", lambda _worktree: True)
    monkeypatch.setattr(
        degodify, "_fallow_json",
        lambda _worktree, command, *_args, **_kwargs: INSPECT if command == "inspect" else {"verdict": "pass"},
    )
    monkeypatch.setattr(degodify, "_gh", lambda _repo, *args, **_kwargs: created.append(args) or "https://example.test/pr/1")
    candidate = degodify.DegodifyFile(TARGET, 1000, 1000, "Utility / Module", "CRITICAL")

    events = []
    result = degodify._run_degodify_impl(
        repo, dry_run=False, supplied_candidate=candidate, log_path=tmp_path / "log",
        providers=["codex", "muse"] if recovery == "fallback" else ["codex"],
        model="decomposition-model", reasoning_effort="high",
        on_event=lambda event, payload: events.append((event, payload)),
    )
    assert all(event == "agent_attempt" for event, _ in events)
    for provider_budgets in budgets.values():
        assert provider_budgets == sorted(provider_budgets, reverse=True)
        if len(provider_budgets) > 1:
            assert provider_budgets[-1] < provider_budgets[0]
    if len(budgets) > 1:
        # A fallback provider gets its own full budget, not what the primary left over.
        assert budgets["muse"][0] > 0
    if recovery in {"exhausted", "timeout"}:
        assert not result.succeeded and not created
        assert len(prompts) == (1 if recovery == "timeout" else 2)
        assert "no clean committed change" in result.reason
        return
    assert len(prompts) == {"immediate": 1, "noop": 2, "dirty": 2, "fallback": 3}[recovery]
    assert events[-1][1]["committed_change"] is True

    assert "- huge at line 40" in prompts[0]
    assert result.succeeded is expect_pr
    if expect_pr:
        args = created[0]
        assert args[args.index("--title") + 1] == "♻️ Curator: [degodify] decompose large.ts (1000 → 200 lines)"
        body = args[args.index("--body") + 1]
        assert "| Lines in `large.ts` | 1000 | 200 |" in body and "`src/theme.ts` (800 lines)" in body
    else:
        assert created == []
        assert result.reason == "target only shrank from 1000 to 900 lines (needs at least 25%)"


def test_planner_uses_the_repository_analyzer_and_its_catalogue_exemptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/god-file-analysis.ts").write_text("// analyzer")
    report = {"topFiles": [
        {"path": f"{tmp_path}/src/registry.ts", "relativePath": "src/registry.ts", "totalLines": 3760, "codeLines": 3000,
         "type": "Engine Core", "status": "CRITICAL", "isDataCatalog": True},
        {"path": f"{tmp_path}/src/store.svelte.ts", "relativePath": "src/store.svelte.ts", "totalLines": 1220, "codeLines": 1124,
         "type": "Store (State)", "status": "CRITICAL", "isDataCatalog": False},
    ]}

    def fake_run(command, **kwargs):
        json_arg = next(arg for arg in command if arg.startswith("--json-file="))
        Path(json_arg.split("=", 1)[1]).write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(degodify.subprocess, "run", fake_run)
    monkeypatch.setattr(degodify, "active_repository_items", lambda _repo: [])

    selection = degodify.plan_degodify(tmp_path)

    assert selection.candidate is not None
    assert selection.candidate.relative_path == "src/store.svelte.ts"
    assert selection.candidate.file_type == "Store (State)"


def test_planner_falls_back_to_the_built_in_scan_without_an_analyzer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "big.ts").write_text("".join(f"function f{index}() {{ return {index}; }}\n" for index in range(600)))
    monkeypatch.setattr(degodify, "active_repository_items", lambda _repo: [])

    assert degodify.plan_degodify(tmp_path).candidate.relative_path == "big.ts"


def test_a_new_decomposition_waits_while_one_is_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(degodify, "open_degodify_pull_request", lambda _repo: "https://example.test/pr/7")
    monkeypatch.setattr(degodify, "run_agent", lambda *_args, **_kwargs: pytest.fail("agent must not run"))
    candidate = DegodifyFile(TARGET, 1000, 1000, "Utility / Module", "CRITICAL")

    result = degodify._run_degodify_impl(tmp_path, dry_run=False, supplied_candidate=candidate)

    assert result.succeeded is True and result.pull_request_url is None and result.branch is None
    assert result.reason == "waiting for open degodify PR https://example.test/pr/7"


def test_open_degodify_pull_request_matches_the_curator_branch_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    pulls = [{"headRefName": "feature/x", "url": "u1"}, {"headRefName": "curator/degod-store-1", "url": "u2"}]
    monkeypatch.setattr(degodify, "gh_json", lambda *_args: pulls)
    assert degodify.open_degodify_pull_request(Path(".")) == "u2"
    monkeypatch.setattr(degodify, "gh_json", lambda *_args: pulls[:1])
    assert degodify.open_degodify_pull_request(Path(".")) is None


def test_degodify_timeout_defaults_to_an_hour() -> None:
    from dev_agents.config import PrFixerConfig

    assert PrFixerConfig().degodify_timeout_minutes == 60
    assert PrFixerConfig().degodify_model == "gpt-6.1-sol"
