from dev_agents.pr_fixer.claims import repair_claim


def test_claim_requires_trusted_author_exact_run_and_order() -> None:
    valid = {"id": 1, "body": "/dev-agents claim run-1", "author_association": "COLLABORATOR",
             "user": {"login": "writer"}, "created_at": "2026-01-01T00:00:10Z"}
    opened = 1767225600.0
    assert repair_claim([valid], "run-1", opened) == {
        "login": "writer", "comment_id": 1, "expires_at": opened + 1810,
    }
    for change in [{"body": "/dev-agents claim old-run"}, {"author_association": "NONE"},
                   {"created_at": "2025-12-31T23:59:59Z"}, {"created_at": "invalid"}]:
        assert repair_claim([{**valid, **change}], "run-1", opened) is None
    later = {**valid, "id": 2, "created_at": "2026-01-01T00:00:30Z"}
    assert repair_claim([later, valid], "run-1", opened)["comment_id"] == 1


def test_claim_after_the_wait_window_still_counts_until_repair_starts() -> None:
    late = {"id": 3, "body": "/dev-agents claim run-1", "author_association": "OWNER",
            "user": {"login": "writer"}, "created_at": "2026-01-01T00:03:20Z"}
    assert repair_claim([late], "run-1", 1767225600.0)["comment_id"] == 3
