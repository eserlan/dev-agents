from dev_agents.workflows.degodify import DegodifyFile, run_degodify


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
