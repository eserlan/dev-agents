"""Module-level constants and the shared logger for the pr_fixer package."""

from __future__ import annotations

from pathlib import Path

SHARED_PR_FIX_SKILL = Path(__file__).resolve().parents[3] / "skills/pr-fix/SKILL.md"
PR_FIX_PROVIDER = "codex"

_ACTIONS = {
    "pull_request": {"opened", "reopened", "synchronize", "ready_for_review"},
    "pull_request_review": {"submitted", "edited"},
    "pull_request_review_comment": {"created", "edited"},
    "check_run": {"completed"},
    "issues": {"opened", "reopened", "labeled", "unlabeled", "edited"},
}
MAX_BODY_BYTES = 1_000_000
REVIEW_REPORT_BEGIN = "DEV_AGENTS_REVIEW_REPORT_BEGIN"
REVIEW_REPORT_END = "DEV_AGENTS_REVIEW_REPORT_END"
REVIEW_SKILL_CANDIDATES = (
    ".agent/skills/codex-review/SKILL.md",
    ".codex/skills/codex-review/SKILL.md",
    ".claude/skills/codex-review/SKILL.md",
    ".agents/skills/codex-review/SKILL.md",
)
# One full review, then up to two targeted post-fix verifications. The second targeted round only
# runs when the first one pushed a fix without a clean verdict (see _round_ended_chain).
MAX_INTERNAL_REVIEW_ROUNDS = 3


def _log(message: str) -> None:
    # Shared by the whole daemon (PR fixer, issue fixer, release comms), not just PR-fixing.
    print(f"[dev-agents] {message}", flush=True)
