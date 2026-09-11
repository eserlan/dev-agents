import json

from dev_agents.runtime import JsonStateStore, SqliteStateStore


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


def test_sqlite_state_store_updates_transactionally(tmp_path):
    store = SqliteStateStore(tmp_path / "state.db", "workflow", {})
    store.save({"runs": ["one"]})
    store.save({"runs": ["one", "two"]})
    assert store.load() == {"runs": ["one", "two"]}


def test_sqlite_state_store_claims_event_once(tmp_path):
    store = SqliteStateStore(tmp_path / "state.db", "events", {})
    assert store.claim_once("run-1") is True
    assert store.claim_once("run-1") is False
