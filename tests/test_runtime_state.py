import json
import sqlite3
import subprocess
import sys
from threading import Barrier, Thread

from dev_agents.pr_fixer import _load_state
from dev_agents.runtime import JsonStateStore, SqliteStateStore, StateRepository
from dev_agents.runtime.state import _process_identity


def test_state_store_round_trips_atomically(tmp_path):
    path = tmp_path / "state.json"
    store = JsonStateStore(path, {"version": 1})
    assert store.load() == {"version": 1}
    store.save({"version": 1, "runs": ["abc"]})
    assert json.loads(path.read_text()) == {"version": 1, "runs": ["abc"]}


def test_state_store_uses_default_for_invalid_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json")
    assert JsonStateStore(path, {"version": 1}).load() == {"version": 1}


def test_sqlite_state_store_migrates_legacy_json(tmp_path):
    legacy = tmp_path / "state.json"
    legacy.write_text('{"version": 1, "pullRequests": {"42": ["check:x"]}}')
    store = SqliteStateStore(tmp_path / "state.db", "pr-fixer", {"version": 1}, legacy)
    assert store.load()["pullRequests"]["42"] == ["check:x"]
    assert store.load()["pullRequests"]["42"] == ["check:x"]


def test_pr_fixer_migrates_original_flat_json_shape(tmp_path):
    legacy = tmp_path / "pr-fixer-state.json"
    database = tmp_path / "pr-fixer-state.db"
    legacy.write_text('{"42": ["comment:7"]}')

    assert _load_state(legacy) == {"version": 1, "pullRequests": {"42": ["comment:7"]}}
    assert SqliteStateStore(database, "pr-fixer", {}).load() == {
        "version": 1,
        "pullRequests": {"42": ["comment:7"]},
    }


def test_sqlite_state_store_updates_transactionally(tmp_path):
    store = SqliteStateStore(tmp_path / "state.db", "workflow", {})
    store.save({"runs": ["one"]})
    store.save({"runs": ["one", "two"]})
    assert store.load() == {"runs": ["one", "two"]}


def test_sqlite_state_store_claims_event_once(tmp_path):
    store = SqliteStateStore(tmp_path / "state.db", "events", {})
    assert store.claim_once("run-1") is True
    assert store.claim_once("run-1") is False


def test_state_repository_migrates_schema_and_enables_wal(tmp_path):
    path = tmp_path / "state.db"
    store = StateRepository(path, "demo", tmp_path / "repo")

    claim = store.claim_run("workflow", "run-1", delivery_id="delivery-1", metadata={"log": "x"})
    record = store.complete_run("workflow", "run-1", metadata={"phase": "done"})

    assert claim.claimed is True
    assert record.status == "completed"
    assert record.attempt == 1
    assert {key: value for key, value in record.metadata.items() if not key.startswith("_")} == {
        "log": "x", "phase": "done",
    }
    assert record.metadata["_worker_identity"]["pid"] > 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    assert connection.execute("SELECT status FROM delivery_claims").fetchone()[0] == "completed"
    events = store.list_run_events("workflow", "run-1")
    assert [event.event for event in events] == ["run_started", "run_completed"]


def test_interrupted_worker_is_recovered_and_delivery_can_retry(tmp_path):
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path / "repo")
    store.claim_run("issue-fixer", "run-1", delivery_id="delivery-1")
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        store.update_run("issue-fixer", "run-1", metadata={"_worker_identity": _process_identity(worker.pid)})
        assert store.recover_interrupted_runs() == []
        assert not store.claim_run("issue-fixer", "run-1", delivery_id="delivery-1").claimed
        worker.terminate()
        worker.wait(timeout=5)
        recovered = store.recover_interrupted_runs()
        assert len(recovered) == 1
        assert recovered[0].status == "failed"
        assert recovered[0].metadata["interrupted"] is True
        assert "worker_interrupted" in [event.event for event in store.list_run_events("issue-fixer", "run-1")]
        retry = store.claim_run("issue-fixer", "run-1", delivery_id="delivery-1")
        assert retry.claimed and retry.record.attempt == 2
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_pid_reuse_cannot_keep_an_abandoned_claim_running(tmp_path):
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path / "repo")
    claim = store.claim_run("pr-review", "run-1")
    identity = dict(claim.record.metadata["_worker_identity"])
    identity["boot_id"] = "a-different-boot"
    identity["start_ticks"] = -1
    store.update_run("pr-review", "run-1", metadata={"_worker_identity": identity})
    retry = store.claim_run("pr-review", "run-1")
    assert retry.claimed and retry.record.attempt == 2


def test_state_repository_syncs_gallery_inventory_and_marks_stale_assets_inactive(tmp_path):
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path / "repo")
    first = [
        {"key": "og/old.jpg", "url": "https://assets.codexcryptica.com/og/old.jpg", "purpose": "Old", "size": "10 KB"},
        {"key": "announcements/new.png", "url": "https://assets.codexcryptica.com/announcements/new.png", "purpose": "New", "size": "20 KB"},
    ]
    assert store.sync_gallery_assets(first) == 2
    assert [asset.key for asset in store.list_gallery_assets()] == [
        "announcements/new.png",
        "og/old.jpg",
    ]

    store.sync_gallery_assets([first[1]])
    assert [asset.key for asset in store.list_gallery_assets()] == ["announcements/new.png"]
    store.sync_gallery_assets(first)
    assert [asset.key for asset in store.list_gallery_assets()] == [
        "announcements/new.png",
        "og/old.jpg",
    ]


def test_state_repository_records_publication_receipts(tmp_path):
    database = tmp_path / "state.db"
    store = StateRepository(database, "demo", tmp_path / "repo")
    store.claim_run("release-comms", "release-1")

    first = store.record_publication(
        "release-comms",
        "release-1",
        "instagram",
        destination="codexcryptica",
        page_url="https://codexcryptica.com/answers/example",
        public_url="https://www.instagram.com/p/example/",
        external_id="media-1",
    )
    store.record_publication(
        "release-comms",
        "release-1",
        "instagram",
        destination="codexcryptica",
        page_url="https://codexcryptica.com/answers/example",
        public_url="https://www.instagram.com/p/updated/",
        external_id="media-2",
    )

    publications = store.list_run_publications("release-comms", "release-1")
    assert first.channel == "instagram"
    assert len(publications) == 1
    assert publications[0].public_url == "https://www.instagram.com/p/updated/"
    assert publications[0].external_id == "media-2"


def test_state_repository_claim_is_atomic_across_workers(tmp_path):
    path = tmp_path / "state.db"
    workers = [StateRepository(path, "demo", tmp_path / "repo") for _ in range(8)]
    barrier = Barrier(len(workers))
    claimed: list[bool] = []

    def claim(store: StateRepository) -> None:
        barrier.wait()
        claimed.append(store.claim_run("workflow", "same-run", delivery_id="same-delivery").claimed)

    threads = [Thread(target=claim, args=(store,)) for store in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert claimed.count(True) == 1
    assert claimed.count(False) == len(workers) - 1


def test_state_repository_retries_failed_run_and_tracks_attempt(tmp_path):
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path / "repo")
    assert store.claim_run("workflow", "run-1").claimed is True
    assert store.complete_run("workflow", "run-1", status="failed", error="timeout").attempt == 1
    retry = store.claim_run("workflow", "run-1")

    assert retry.claimed is True
    assert retry.record.attempt == 2
    assert retry.record.error is None


def test_state_repository_tracks_feedback_idempotency_keys(tmp_path):
    store = StateRepository(tmp_path / "state.db", "demo", tmp_path / "repo")
    assert store.unprocessed_feedback("pr-fixer", "42", ["comment:1", "check:2"]) == [
        "check:2",
        "comment:1",
    ]
    store.mark_feedback_processed("pr-fixer", "42", ["comment:1"])

    assert store.unprocessed_feedback("pr-fixer", "42", ["comment:1", "check:2"]) == ["check:2"]


def test_state_repository_quarantines_corrupt_database(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"not a sqlite database")

    store = StateRepository(path, "demo", tmp_path / "repo")

    assert store.claim_run("workflow", "run-1").claimed is True
    assert list(tmp_path.glob("state.db.corrupt-*"))
