"""Explicit feature attribution and receipt-backed release coverage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from dev_agents.runtime.state import PublicationRecord, StateRepository


def feature_id(feature: Mapping[str, Any]) -> str:
    text = json.dumps({k: feature.get(k, "") for k in ("name", "why_users_care")}, sort_keys=True)
    return "feature-" + hashlib.sha256(text.encode()).hexdigest()[:20]


def feature_ids(draft: Mapping[str, Any]) -> list[str]:
    return sorted(
        {part.strip() for part in str(draft.get("featureIds", "")).split(",") if part.strip()}
    )


def message_id(draft: Mapping[str, Any]) -> str:
    if draft.get("legacyIdentity") == "true":
        return ""
    if draft.get("messageId"):
        return str(draft["messageId"])
    ids = feature_ids(draft)
    if ids:
        return "features:" + ",".join(ids)
    if draft.get("pageUrl"):
        return ""
    # Legacy drafts have no trustworthy feature attribution. Content identity still
    # distinguishes unrelated messages sharing a page or lacking a URL entirely.
    text = str(draft.get("text") or draft.get("title") or draft.get("body") or "")
    return "message-" + hashlib.sha256(text.encode()).hexdigest()[:20]


def coverage_report(
    features: list[dict[str, Any]],
    drafts: Mapping[str, Any],
    receipts: list[PublicationRecord],
    channels: list[str] | None = None,
    destinations: Mapping[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    result = []
    for feature in features:
        key = feature_id(feature)
        variants = [
            {"channel": channel, "message_id": message_id(draft)}
            for channel in ("bluesky", "threads", "github_discussions", "instagram")
            for draft in drafts.get(channel) or []
            if key in feature_ids(draft)
        ]
        published = [
            {
                "channel": receipt.channel,
                "destination": receipt.destination,
                "run_id": receipt.run_id,
                "status": receipt.status,
                "public_url": receipt.public_url,
            }
            for receipt in receipts
            if key in receipt.metadata.get("feature_ids", [])
        ]
        enabled = set(channels or [])
        expansion = {
            "bluesky": {"bluesky", "instagram", "x", "pinterest", "discord"},
            "github_discussions": {"github_discussions", "reddit"},
            "threads": {"threads"},
            "instagram": {"instagram"},
        }
        expected = set().union(*(expansion[v["channel"]] for v in variants)) & enabled
        expected_writes = {
            (channel, destination)
            for channel in expected
            for destination in (destinations or {}).get(channel, [channel])
        }
        delivered_writes = {
            (r["channel"], r["destination"]) for r in published if r["status"] == "published"
        }
        delivered = {channel for channel, _ in delivered_writes}
        missing_writes = expected_writes - delivered_writes
        missing = {channel for channel, _ in missing_writes}
        status = "uncovered" if not variants else "drafted"
        if delivered:
            status = "partially_published" if missing else "published"
        elif any(r["status"] == "staged" for r in published):
            status = "staged"
        elif any(r["status"] == "pending" for r in published):
            status = "pending"
        result.append(
            {
                "feature_id": key,
                "name": feature.get("name", ""),
                "drafts": variants,
                "receipts": published,
                "expected_channels": sorted(expected),
                "missing_channels": sorted(missing),
                "missing_destinations": [
                    f"{channel}:{destination}" for channel, destination in sorted(missing_writes)
                ],
                "status": status,
            }
        )
    return result


def prepare_messages(drafts: Mapping[str, Any]) -> None:
    """Give legacy blank-URL channel variants the same stable message identity."""
    bases = drafts.get("bluesky") or drafts.get("github_discussions") or []
    for channel in ("bluesky", "github_discussions", "threads", "instagram"):
        for index, draft in enumerate(drafts.get(channel) or []):
            if (
                not isinstance(draft, dict)
                or feature_ids(draft)
                or draft.get("pageUrl")
                or draft.get("legacyIdentity")
            ):
                continue
            base = bases[index] if index < len(bases) and channel != "instagram" else draft
            draft.setdefault("messageId", message_id(base))


def refresh_coverage(repository: StateRepository, run_id: str) -> None:
    run = repository.get_run("release-comms", run_id)
    inventory = run.metadata.get("feature_inventory", {}) if run else {}
    if inventory:
        receipts = {
            (
                receipt.run_id,
                receipt.channel,
                receipt.destination,
                receipt.page_url,
                receipt.message_id,
            ): receipt
            for feature in inventory["features"]
            for receipt in repository.list_feature_publications(feature_id(feature))
        }
        coverage = coverage_report(
            inventory["features"],
            inventory["drafts"],
            list(receipts.values()),
            inventory["channels"],
            inventory.get("destinations"),
        )
        repository.update_run("release-comms", run_id, metadata={"feature_coverage": coverage})
