from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from dev_agents.cli import build_parser, main


def _install_fakes(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    monkeypatch.setattr("dev_agents.cli.load_projects_config", lambda path: SimpleNamespace())
    monkeypatch.setattr("dev_agents.cli.select_project", lambda config, name: SimpleNamespace())

    def fake_run_release_comms(
        project: Any,
        project_name: str,
        promote_run_id: str,
        *,
        dry_run: bool,
        publish_approved: bool,
        **kwargs: Any,
    ) -> Any:
        captured.update(dry_run=dry_run, publish_approved=publish_approved)
        return SimpleNamespace(
            promote_run_id=promote_run_id,
            new_sha="abc123",
            previous_sha=None,
            postworthy=True,
            drafts={},
            published={},
            completed=True,
        )

    monkeypatch.setattr("dev_agents.cli.run_release_comms", fake_run_release_comms)


@pytest.mark.parametrize(
    ("extra", "dry_run", "publish_approved"),
    [
        ([], True, False),
        (["--publish"], False, True),
        (["--no-dry-run"], False, False),
        (["--publish", "--dry-run"], True, True),
    ],
)
def test_release_comms_evaluate_dry_run_semantics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    dry_run: bool,
    publish_approved: bool,
) -> None:
    captured: dict[str, Any] = {}
    _install_fakes(monkeypatch, captured)

    assert main(["release-comms", "evaluate", "proj", "7", *extra]) == 0
    assert captured == {"dry_run": dry_run, "publish_approved": publish_approved}
    assert json.loads(capsys.readouterr().out)["promoteRunId"] == "7"


@pytest.mark.parametrize(
    ("extra", "local_generation"),
    [
        ([], None),
        (["--local-generation"], True),
        (["--no-local-generation"], False),
    ],
)
def test_release_comms_evaluate_local_generation_flag(
    extra: list[str], local_generation: bool | None
) -> None:
    args = build_parser().parse_args(["release-comms", "evaluate", "proj", "7", *extra])
    assert args.local_generation is local_generation
