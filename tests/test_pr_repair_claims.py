from dev_agents.pr_fixer.claims import repair_claim


def test_claim_requires_trusted_author_exact_run_and_window() -> None:
    valid = {"id": 1, "body": "/dev-agents claim run-1", "author_association": "COLLABORATOR",
             "user": {"login": "writer"}, "created_at": "2026-01-01T00:00:10Z"}
    opened = 1767225600.0
    assert repair_claim([valid], "run-1", opened, 60) == {
        "login": "writer", "comment_id": 1, "expires_at": opened + 1810,
    }
    for change in [{"body": "/dev-agents claim old-run"}, {"author_association": "NONE"},
                   {"created_at": "2026-01-01T00:01:01Z"},
                   {"created_at": "2025-12-31T23:59:59Z"}, {"created_at": "invalid"}]:
        assert repair_claim([{**valid, **change}], "run-1", opened, 60) is None
    later = {**valid, "id": 2, "created_at": "2026-01-01T00:00:30Z"}
    assert repair_claim([later, valid], "run-1", opened, 60)["comment_id"] == 1
