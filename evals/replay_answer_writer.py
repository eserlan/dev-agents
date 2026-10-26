"""Replay frozen Answer briefs through the writer, optionally scored by the reviewer.

Each case drafts in a detached temporary worktree of the target repository.
The replay never pushes, never opens a PR, and forbids image uploads: the
writer must use a placeholder ``seo.image`` URL instead of the R2 workflow.
Drafts, agent logs and (with ``--score``) reviewer assessments are saved in
the output directory. This is the writer-side counterpart to
``replay_answer_quality.py``, which only evaluates the reviewer.

```bash
.venv/bin/python evals/replay_answer_writer.py \
  --config ~/.config/dev-agents/projects.yaml --project codex-cryptica \
  --output ~/.local/state/dev-agents/codex-cryptica/evals/writer-1 --score
```

``--dry-run`` resolves the provider, model and effort for each case without
starting an agent or touching the target repository. Use it to confirm the
run is pinned to the intended settings (for example muse-wonder-1.0 at high
effort, or a medium-effort comparison run) before spending a live replay.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from dev_agents.config import ProjectConfig, load_projects_config, select_project
from dev_agents.issue_fixer import _prompt
from dev_agents.runtime import run_agent

REPLAY_CONSTRAINT = """

Replay constraint (overrides the article skill where they conflict): this is an
offline evaluation in a detached worktree. Do not push, do not create branches
or pull requests, and do not commit; leave the finished article as uncommitted
files in this worktree. Do not generate or upload images and do not call any
external publishing tool: set seo.image to the placeholder
https://assets.codexcryptica.com/og/<slug>.jpg without verifying it. Run the
repository's local answer validation commands that work offline and report any
check you skipped in the final report."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _writer_settings(
    project: ProjectConfig, args: argparse.Namespace
) -> tuple[str, str | None, str | None]:
    """Resolve provider/model/effort: CLI first, then the project's answer queue."""
    queues = [*project.issue_fixers, *([project.issue_fixer] if project.issue_fixer else [])]
    answer = next((queue for queue in queues if queue.kind == "answer"), None)
    provider = args.provider or (answer.provider if answer else "muse")
    model = args.model or (answer.model if answer else None)
    effort = args.reasoning_effort or (answer.reasoning_effort if answer else None)
    if effort is None and project.pr_fixer is not None:
        effort = project.pr_fixer.reasoning_effort
    return provider, model, effort


def _changed_pages(worktree: Path, page_globs: list[str]) -> list[str]:
    import fnmatch

    status = _git(worktree, "status", "--porcelain")
    pages = []
    for line in status.splitlines():
        path = line[3:].strip()
        if any(fnmatch.fnmatchcase(path, glob) for glob in page_globs):
            pages.append(path)
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--cases", type=Path, default=Path(__file__).parent / "answer-writing/cases.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="+", help="Run only the named manifest cases")
    parser.add_argument("--provider", help="Override the answer queue's provider")
    parser.add_argument("--model", help="Override the answer queue's model")
    parser.add_argument(
        "--reasoning-effort", help="Override the writing effort (for example high vs medium)"
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="Score each draft with the project's copy reviewer and check expected_verdict",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved settings per case without running any agent",
    )
    args = parser.parse_args()
    project = select_project(load_projects_config(args.config), args.project)
    if args.score and project.copy_review is None:
        parser.error("--score requires the project to configure copy_review")
    provider, model, effort = _writer_settings(project, args)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.only:
        cases = [case for case in cases if case["name"] in args.only]
        if len(cases) != len(set(args.only)):
            parser.error("--only contains a case absent from the manifest")
    settings_line = f"provider={provider} model={model} reasoning_effort={effort}"
    if args.dry_run:
        for case in cases:
            print(f"{case['name']}: {settings_line} expected_slug={case['expected_slug']}")
        return
    args.output.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for case in cases:
        name = case["name"]
        result: dict[str, Any] = {"name": name, "settings": settings_line}
        worktree: Path | None = None
        try:
            worktree = Path(tempfile.mkdtemp(prefix=f"writer-{name}-", dir=args.output))
            _git(project.repo, "worktree", "add", "--detach", str(worktree), "HEAD")
            issue = {
                "number": 0,
                "title": case["title"],
                "body": case["brief"],
                "url": "",
                "comments": [],
            }
            prompt = (
                _prompt(worktree, issue, "HEAD", f"eval/answer-{name}", [], kind="answer")
                + REPLAY_CONSTRAINT
            )
            agent = run_agent(
                provider,
                prompt,
                cwd=worktree,
                log_path=args.output / f"{name}.log",
                timeout_seconds=60 * 60,
                reasoning_effort=effort,
                model=model,
                json_events=provider == "muse",
            )
            result["agent_exit"] = agent.returncode
            result["timed_out"] = agent.timed_out
            page_globs = (
                project.copy_review.page_globs
                if project.copy_review
                else ["apps/web/src/lib/content/answers/pages/*.ts"]
            )
            pages = _changed_pages(worktree, page_globs)
            result["pages"] = pages
            for page in pages:
                draft = args.output / f"{name}.draft{Path(page).suffix}"
                shutil.copyfile(worktree / page, draft)
                result["draft"] = str(draft)
            expected = f"{case['expected_slug']}.ts"
            result["page_created"] = any(Path(page).name == expected for page in pages)
            if args.score and pages and project.copy_review is not None:
                from dev_agents.copy_review import review_answer_draft

                assessment = review_answer_draft(
                    project,
                    project.copy_review,
                    worktree=worktree,
                    page_paths=pages,
                    issue=issue,
                    log_path=args.output / f"{name}.review.log",
                )
                (args.output / f"{name}.assessment.json").write_text(
                    assessment.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                result["verdict"] = assessment.verdict
                result["score"] = assessment.score
            passed = (
                agent.returncode == 0
                and not agent.timed_out
                and result["page_created"]
                and (
                    not args.score
                    or result.get("verdict") == case.get("expected_verdict", "clean")
                )
            )
            result["status"] = "passed" if passed else "failed"
        except Exception as error:  # noqa: BLE001 - preserve a result for every case
            result["status"] = "failed"
            result["reason"] = str(error)
        finally:
            if worktree is not None:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=project.repo,
                    capture_output=True,
                    check=False,
                )
                shutil.rmtree(worktree, ignore_errors=True)
        results.append(result)
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    if any(result["status"] == "failed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
