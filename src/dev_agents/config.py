"""Loading and validation for local target-repository configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProjectConfig(BaseModel):
    """A target repository available to workflows."""

    model_config = ConfigDict(frozen=True)

    repo: Path
    github: str | None = None
    pr_fixer: PrFixerConfig | None = None
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
    timeout_minutes: int = 20
    reconcile_interval_seconds: int = 300
    auto_merge: bool = False
    log_retention_days: int = 30
    worktree_retention_days: int = 2
    auto_merge_quiet_seconds: int = 60
    heartbeat_seconds: int = 30
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
