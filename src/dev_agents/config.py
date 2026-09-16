"""Loading and validation for local target-repository configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class IssueFixerConfig(BaseModel):
    """Opt-in configuration for the label-driven issue fixer."""

    model_config = ConfigDict(frozen=True)

    label: str = "bug"
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
    report_vercel_min_interval_seconds: int = 300
    report_vercel_max_deployments_24h: int = 80
    pr_fixer: PrFixerConfig | None = None
    issue_fixer: IssueFixerConfig | None = None
    release_comms: ReleaseCommsConfig | None = None


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
    log_retention_days: int = 30
    worktree_retention_days: int = 2
    auto_merge_quiet_seconds: int = 60
    heartbeat_seconds: int = 30
    pause_on_external_agent_commits: bool = True
    external_agent_logins: list[str] = ["google-labs-jules[bot]"]
    external_agent_resume_label: str = "dev-agents-resume"
    degodify_webhook_path: str = "/degodify"
    degodify_webhook_secret_env: str = "DEGODIFY_WEBHOOK_SECRET"


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
