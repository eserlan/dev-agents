import os
import time

from dev_agents.runtime import remove_older_than


def test_remove_older_than_removes_only_expired_files(tmp_path):
    old = tmp_path / "pr-old.log"
    fresh = tmp_path / "pr-fresh.log"
    old.write_text("old")
    fresh.write_text("fresh")
    old_time = time.time() - 3600
    os.utime(old, (old_time, old_time))
    assert remove_older_than(tmp_path, "pr-*.log", 60) == 1
    assert not old.exists()
    assert fresh.exists()
