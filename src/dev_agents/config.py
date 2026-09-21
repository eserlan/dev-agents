"""Loading and validation for local target-repository configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class IssueFixerConfig(BaseModel):
    """Opt-in configuration for one label-driven issue queue.

    A project can run several of these concurrently (see
    ``ProjectConfig.issue_fixers``) -- e.g. a "bug" queue and a separate
    "gui-fix" queue for GUI enhancements -- each with its own label, branch
    prefix, and framing.
    """

    model_config = ConfigDict(frozen=True)

    label: str = "bug"
    # "fix" frames the work as fixing a defect; "enhancement" frames it as
    # implementing an improvement. Drives prompt wording and the PR title
    # prefix ("fix: ..." vs "feat: ..."), not the validation/review pipeline,
    # which is identical either way.
    kind: Literal["fix", "enhancement"] = "fix"
    base_branch: str = "main"
    branch_prefix: str = "dev-agents/issue-"
    timeout_minutes: int = 30
    max_open_issues: int = 5


class ProjectConfig(BaseModel):
    """A target repository available to workflows."""

    model_config = ConfigDict(frozen=True)

    repo: Path
    github: str | None = None
    visualization_path: Path | None = None
    report_vercel_project: str | None = None
    report_vercel_scope: str | None = None
    report_vercel_alias: str | None = None
    report_vercel_token_env: str = "VERCEL_TOKEN"
    report_vercel_timeout_seconds: int = 120
    report_vercel_min_interval_seconds: int = 3600
    report_vercel_max_deployments_24h: int = 24
    pr_fixer: PrFixerConfig | None = None
    # Kept for backward compatibility with existing single-queue configs;
    # combined with issue_fixers (below) at daemon startup into one list.
    issue_fixer: IssueFixerConfig | None = None
    # Additional label-driven queues beyond the single `issue_fixer` above,
    # e.g. a "bug" queue plus a "gui-fix" enhancement queue running together.
    issue_fixers: list[IssueFixerConfig] = []
    release_comms: ReleaseCommsConfig | None = None


class ContentQueueConfig(BaseModel):
    """Opt-in configuration for cadence-driven social posts from a target
    repo's own marketing backlog file (see ``dev_agents.workflows.content_queue``),
    as distinct from release-comms's deploy-triggered drafting."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    log_path: str = ".social/bluesky-posts.md"
    rules_issue: int | None = None
    # How often the daemon checks whether today's post is still due -- not the
    # posting cadence itself, which stays at most once/day via claim_run.
    poll_seconds: int = 600
    # Whether the scheduler may also turn a bare backlog item into a new
    # Drafted entry (never auto-published) when nothing is ready to publish.
    auto_draft: bool = False


class ReleaseCommsConfig(BaseModel):
    """Opt-in configuration for the release communications workflow."""

    model_config = ConfigDict(frozen=True)

    tracking_issue: int = 2906
    providers: list[str] = ["muse", "claude", "agy", "codex"]
    timeout_minutes: int = 10
    state_path: Path | None = None
    log_dir: Path | None = None
    auto_publish: bool = False
    webhook_path: str = "/release-comms"
    webhook_secret_env: str = "RELEASE_COMMS_SECRET"
    local_generation: bool = False
    forms: list[str] = ["short", "long"]
    destinations: list[str] = [
        "bluesky",
        "discord",
        "github_discussions",
        "instagram",
        "x",
        "reddit",
    ]
    subreddit: str = "codexcryptica"
    image_generation: bool = False
    image_providers: list[str] = ["agy", "codex", "muse"]
    publication_delay_min_seconds: float = 900.0
    publication_delay_max_seconds: float = 1800.0
    scheduler_poll_seconds: int = 30
    content_queue: ContentQueueConfig | None = None


class PrFixerConfig(BaseModel):
    """Opt-in configuration for the webhook-driven PR fixer."""

    base_branch: str = "staging"
    port: int = 8788
    webhook_path: str = "/github"
    webhook_secret_env: str = "GITHUB_WEBHOOK_SECRET"
    state_path: Path | None = None
    log_dir: Path | None = None
    worktree_dir: Path | None = None
    providers: list[str] = ["codex"]
    reasoning_effort: str = "high"
    timeout_minutes: int = 20
    reconcile_interval_seconds: int = 300
    review_without_copilot: bool = True
    auto_merge: bool = False
    auto_merge_issue_fixes_only: bool = False
    log_retention_days: int = 30
    worktree_retention_days: int = 2
    # Shared cap on simultaneous worktrees (PR review/fix + issue fix combined) for
    # this project: each one runs a full validation build alongside its agent, and
    # several running at once can oversubscribe a small machine's CPU well past its
    # core count.
    max_concurrent_worktree_runs: int = 2
    auto_merge_quiet_seconds: int = 60
    heartbeat_seconds: int = 30
    pause_on_external_agent_commits: bool = True
    external_agent_logins: list[str] = ["google-labs-jules[bot]"]
    external_agent_resume_label: str = "dev-agents-resume"
    degodify_webhook_path: str = "/degodify"
    degodify_webhook_secret_env: str = "DEGODIFY_WEBHOOK_SECRET"
    # After this many consecutive failed attempts at the same run_id (e.g. a
    # provider quota outage), auto-apply the "paused" label instead of letting
    # reconcile retry-and-comment on it forever.
    max_consecutive_failures: int = 3


class ProjectsConfig(BaseModel):
    """All locally configured target repositories, keyed by project name."""

    model_config = ConfigDict(frozen=True)

    projects: dict[str, ProjectConfig] = Field(min_length=1)


class ConfigError(ValueError):
    """Configuration could not be loaded or did not contain a requested project."""


def load_projects_config(path: Path) -> ProjectsConfig:
    """Load a YAML configuration file without accessing configured repositories."""
    if not path.is_file():
        raise ConfigError(f"Configuration file does not exist: {path}")

    try:
        with path.open(encoding="utf-8") as file:
            data = yaml.safe_load(file)
        return ProjectsConfig.model_validate(data)
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise ConfigError(f"Invalid configuration at {path}: {error}") from error


def select_project(config: ProjectsConfig, name: str) -> ProjectConfig:
    """Return a configured project by name with a helpful error when absent."""
    try:
        return config.projects[name]
    except KeyError as error:
        available = ", ".join(sorted(config.projects))
        raise ConfigError(f"Unknown project {name!r}. Available projects: {available}") from error
