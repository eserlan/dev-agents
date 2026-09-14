import json
import sqlite3
from threading import Barrier, Thread

from dev_agents.pr_fixer import _load_state
from dev_agents.runtime import JsonStateStore, SqliteStateStore, StateRepository


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
    assert record.metadata == {"log": "x", "phase": "done"}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert connection.execute("SELECT status FROM delivery_claims").fetchone()[0] == "completed"
    events = store.list_run_events("workflow", "run-1")
    assert [event.event for event in events] == ["run_started", "run_completed"]


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
