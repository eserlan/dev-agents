from datetime import datetime, timedelta
from pathlib import Path

from dev_agents.runtime.agent import run_agent
from dev_agents.runtime.quota import (
    DEFAULT_BLOCK,
    quota_blocked_until,
    record_quota_block,
    usage_limit_reset,
)

NOW = datetime(2026, 10, 29, 19, 15).astimezone()
CODEX_LIMIT = (
    "ERROR: You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/settings/usage to purchase more credits or try again at 11:20 PM."
)


def test_usage_limit_reset_reads_the_quoted_local_time() -> None:
    assert usage_limit_reset(CODEX_LIMIT, NOW) == NOW.replace(hour=23, minute=20)
    assert usage_limit_reset("hit your usage limit ... try again at 9:05 AM", NOW) == (
        NOW.replace(hour=9, minute=5) + timedelta(days=1)
    )
    assert usage_limit_reset("You've hit your usage limit.", NOW) == NOW + DEFAULT_BLOCK
    assert usage_limit_reset("error: network failure", NOW) is None


def test_quota_block_lasts_until_the_reset_time(isolated_quota: Path) -> None:
    assert quota_blocked_until("codex", NOW) is None
    record_quota_block("codex", NOW + timedelta(hours=1))

    assert quota_blocked_until("codex", NOW) == NOW + timedelta(hours=1)
    assert quota_blocked_until("muse", NOW) is None
    assert quota_blocked_until("codex", NOW + timedelta(hours=2)) is None


def test_run_agent_records_a_usage_limit_refusal(monkeypatch, tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text("an earlier run also hit your usage limit\n", encoding="utf-8")
    script = tmp_path / "limit.txt"
    script.write_text(CODEX_LIMIT, encoding="utf-8")
    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command",
        lambda *_args, **_kwargs: ["sh", "-c", f"cat {script}; exit 1"],
    )

    result = run_agent("codex", "x", cwd=tmp_path, log_path=log, timeout_seconds=5)

    assert result.quota_exhausted
    assert quota_blocked_until("codex") is not None


def test_run_agent_ignores_limits_reported_by_earlier_runs(monkeypatch, tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text(CODEX_LIMIT + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "dev_agents.runtime.agent.provider_command",
        lambda *_args, **_kwargs: ["sh", "-c", "echo boom; exit 1"],
    )

    result = run_agent("codex", "x", cwd=tmp_path, log_path=log, timeout_seconds=5)

    assert not result.quota_exhausted
    assert quota_blocked_until("codex") is None
