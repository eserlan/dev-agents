import json

from dev_agents.runtime import JsonStateStore


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
