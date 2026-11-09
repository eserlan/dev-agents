import fcntl
from pathlib import Path

from dev_agents.visualize_refresh import _coalesced


def test_refresh_skips_when_another_worker_holds_the_lock(tmp_path: Path) -> None:
    output = tmp_path / "report.html"
    lock_path = tmp_path / ".report.html.refresh.lock"
    calls: list[int] = []
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        _coalesced(output, lambda: calls.append(1))
    assert calls == []
    # The request is left for the lock holder to pick up.
    assert (tmp_path / ".report.html.refresh-pending").exists()


def test_refresh_reruns_once_for_requests_arriving_mid_refresh(tmp_path: Path) -> None:
    output = tmp_path / "report.html"
    pending = tmp_path / ".report.html.refresh-pending"
    calls: list[int] = []

    def refresh() -> None:
        calls.append(1)
        if len(calls) == 1:
            # Two more jobs finish while the first refresh runs; they collapse into one.
            pending.touch()
            pending.touch()

    _coalesced(output, refresh)
    assert len(calls) == 2
    assert not pending.exists()
