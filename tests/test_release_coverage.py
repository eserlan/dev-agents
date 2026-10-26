import sqlite3
from pathlib import Path

import pytest

from dev_agents.config import ProjectConfig
from dev_agents.runtime import StateRepository
from dev_agents.workflows.release_coverage import coverage_report, feature_id, refresh_coverage
from dev_agents.workflows.release_publish import (
    PublicationReceipt,
    pending_publication_count,
    publication_key,
    publish_release_drafts,
)


@pytest.mark.parametrize("page", ["", "https://example.test/shared"])
def test_features_sharing_a_page_keep_distinct_receipts_and_resume(
    tmp_path: Path, monkeypatch, page: str
) -> None:
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path)
    store.claim_run("release-comms", "release")
    features = [{"name": "Shop"}, {"name": "Onboarding"}]
    drafts = {
        "bluesky": [
            {
                "pageUrl": page,
                "text": f["name"],
                "image": "https://example.test/image.png",
                "featureIds": feature_id(f),
            }
            for f in features
        ]
    }
    posts = []

    def publish(**kwargs):
        posts.append(kwargs["text"])
        return PublicationReceipt("bluesky", "bluesky", page, f"https://social.test/{len(posts)}")

    monkeypatch.setattr("dev_agents.workflows.release_publish.publish_bluesky", publish)

    def persist(receipt):
        store.record_publication(
            "release-comms",
            "release",
            receipt.channel,
            destination=receipt.destination,
            page_url=receipt.page_url,
            message_id=receipt.message_id,
            metadata=receipt.metadata,
            public_url=receipt.public_url,
        )

    project = ProjectConfig(repo=tmp_path, github="owner/repo")
    kwargs = {
        "project": project,
        "drafts": drafts,
        "recommended_channels": ["bluesky"],
        "env": {},
        "dry_run": False,
        "on_receipt": persist,
        "publication_delay_min_seconds": 0,
        "publication_delay_max_seconds": 0,
        "max_publications": 1,
    }
    _, errors = publish_release_drafts(**kwargs, already_published=set())
    assert errors == [] and posts == ["Shop"]
    restarted = StateRepository(tmp_path / "state.db", "demo", tmp_path)
    receipts = restarted.list_run_publications("release-comms", "release")
    keys = {publication_key(r.channel, r.destination, r.page_url, r.message_id) for r in receipts}
    assert (
        pending_publication_count(
            repo=tmp_path, drafts=drafts, recommended_channels=["bluesky"], already_published=keys
        )
        == 1
    )
    _, errors = publish_release_drafts(**kwargs, already_published=keys)
    assert errors == [] and posts == ["Shop", "Onboarding"]
    receipts = restarted.list_run_publications("release-comms", "release")
    assert len(receipts) == 2
    report = coverage_report(features, drafts, receipts, ["bluesky", "instagram"])
    assert [r["status"] for r in report] == ["partially_published", "partially_published"]
    assert all(r["missing_channels"] == ["instagram"] for r in report)
    assert len(restarted.list_feature_publications(feature_id(features[0]))) == 1
    restarted.claim_run(
        "release-comms",
        "later",
        metadata={
            "feature_inventory": {
                "features": features,
                "drafts": drafts,
                "channels": ["bluesky"],
            }
        },
    )
    refresh_coverage(restarted, "later")
    coverage = restarted.get_run("release-comms", "later").metadata["feature_coverage"]
    assert all(row["status"] == "published" for row in coverage)
    assert all(row["receipts"][0]["run_id"] == "release" for row in coverage)


def test_unattributed_draft_does_not_count_as_feature_coverage(tmp_path: Path) -> None:
    report = coverage_report(
        [{"name": "Shop"}], {"bluesky": [{"text": "Shop", "pageUrl": ""}]}, [], ["bluesky"]
    )
    assert report[0]["status"] == "uncovered"


def test_schema_upgrade_preserves_legacy_receipts_without_attribution(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateRepository(path, "demo", tmp_path)
    store.claim_run("release-comms", "old")
    store.record_publication(
        "release-comms", "old", "bluesky", public_url="https://social.test/old"
    )
    # Reconstruct the v5 table and version, as on an existing daemon installation.
    with sqlite3.connect(path) as db:
        db.executescript("""
          ALTER TABLE publications RENAME TO new_publications;
          DROP INDEX publications_lookup_idx;
        """)
        StateRepository._migrate_to_v4(db)
        db.execute(
            "INSERT INTO publications SELECT project_name,workflow,run_id,channel,destination,page_url,public_url,external_id,status,error,published_at,metadata FROM new_publications"
        )
        db.execute("DROP TABLE new_publications")
        db.execute("UPDATE schema_version SET version=5")
    upgraded = StateRepository(path, "demo", tmp_path)
    old = upgraded.list_run_publications("release-comms", "old")[0]
    assert old.public_url == "https://social.test/old" and old.message_id == ""
    assert old.metadata == {}


def test_legacy_queued_run_does_not_replay_or_infer_coverage(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import asdict

    from dev_agents.config import ReleaseCommsConfig
    from dev_agents.workflows.release_comms import run_release_comms
    from dev_agents.workflows.release_content import EvaluatorResult, WriterResult

    database = tmp_path / "state.db"
    project = ProjectConfig(
        repo=tmp_path,
        github="owner/repo",
        release_comms=ReleaseCommsConfig(
            state_path=database, destinations=["bluesky"], image_generation=False
        ),
    )
    store = StateRepository(database, "demo", tmp_path)
    evaluation = EvaluatorResult(
        postworthy=True,
        reason="Launch",
        features=[{"name": "Shop"}],
        recommended_channels=["bluesky"],
    )
    drafts = WriterResult(
        bluesky=[{"text": "Shop", "pageUrl": "", "image": "https://example.test/art.png"}]
    )
    store.claim_run("release-comms", "old")
    store.record_publication("release-comms", "old", "bluesky", destination="bluesky")
    store.record_publication("release-comms", "old", "discord", destination="main-community")
    store.complete_run(
        "release-comms",
        "old",
        status="failed",
        metadata={
            "_release_comms_resume": {
                "evaluator_result": asdict(evaluation),
                "writer_result": asdict(drafts),
                "new_sha": "new",
                "previous_sha": "previous",
            }
        },
    )
    monkeypatch.setattr("dev_agents.workflows.reddit.sync_reddit_status", lambda **kwargs: None)
    monkeypatch.setattr(
        "dev_agents.workflows.release_publish.publish_bluesky",
        lambda **kwargs: pytest.fail("legacy announcement must not be replayed"),
    )
    result = run_release_comms(
        project, "demo", "old", dry_run=False, publish_approved=True, notify_tracking_issue=False
    )
    assert result.completed
    run = store.get_run("release-comms", "old")
    assert run.metadata["feature_coverage"][0]["status"] == "uncovered"
    assert len(store.list_run_publications("release-comms", "old")) == 2


@pytest.mark.parametrize("status", ["pending", "staged", "published"])
def test_pending_and_staged_receipts_are_not_reported_as_published(
    tmp_path: Path, status: str
) -> None:
    feature = {"name": "Point crawl"}
    identity = feature_id(feature)
    drafts = {"github_discussions": [{"title": "Point crawl", "featureIds": identity}]}
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path)
    store.claim_run("release-comms", "release")
    store.record_publication(
        "release-comms",
        "release",
        "reddit",
        destination="reddit",
        message_id="features:" + identity,
        status=status,
        metadata={"feature_ids": [identity]},
    )
    report = coverage_report(
        [feature], drafts, store.list_run_publications("release-comms", "release"), ["reddit"]
    )
    assert report[0]["status"] == status
    assert report[0]["missing_channels"] == ([] if status == "published" else ["reddit"])


def test_a_partial_discord_delivery_is_not_full_feature_coverage(tmp_path: Path) -> None:
    feature = {"name": "Shop"}
    identity = feature_id(feature)
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path)
    store.claim_run("release-comms", "release")
    store.record_publication(
        "release-comms",
        "release",
        "discord",
        destination="main",
        metadata={"feature_ids": [identity]},
    )
    report = coverage_report(
        [feature],
        {"bluesky": [{"text": "Shop", "featureIds": identity}]},
        store.list_run_publications("release-comms", "release"),
        ["discord"],
        {"discord": ["main", "other"]},
    )
    assert report[0]["status"] == "partially_published"
    assert report[0]["missing_destinations"] == ["discord:other"]
