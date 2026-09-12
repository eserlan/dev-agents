from pathlib import Path

import pytest

from dev_agents.config import ConfigError, load_projects_config, select_project


def test_loads_and_selects_project(tmp_path: Path) -> None:
    config_path = tmp_path / "projects.yaml"
    config_path.write_text(
        "projects:\n"
        "  demo:\n"
        "    repo: /tmp/demo\n"
        "    github: owner/demo\n"
        "    release_comms:\n"
        "      tracking_issue: 123\n"
        "      auto_publish: false\n"
    )

    project = select_project(load_projects_config(config_path), "demo")

    assert project.repo == Path("/tmp/demo")
    assert project.github == "owner/demo"
    assert project.release_comms is not None
    assert project.release_comms.tracking_issue == 123
    assert project.release_comms.auto_publish is False


def test_reports_unknown_project(tmp_path: Path) -> None:
    path = tmp_path / "projects.yaml"
    path.write_text("projects:\n  demo:\n    repo: /tmp/demo\n")

    with pytest.raises(ConfigError, match="Unknown project"):
        select_project(load_projects_config(path), "missing")
