"""Explicit, revision-scoped repair claims from trusted PR participants."""

from datetime import datetime
from typing import Any


def repair_claim(comments: list[dict[str, Any]], run_id: str, opened_at: float,
                 window_seconds: int) -> dict[str, Any] | None:
    command = f"/dev-agents claim {run_id}"
    for comment in sorted(comments, key=lambda item: str(item.get("created_at", ""))):
        if comment.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}:
            continue
        if str(comment.get("body", "")).strip() != command:
            continue
        try:
            created = datetime.fromisoformat(comment["created_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if opened_at <= created <= opened_at + window_seconds:
            return {"login": (comment.get("user") or {}).get("login"),
                    "comment_id": comment.get("id"), "expires_at": created + 1800}
    return None
