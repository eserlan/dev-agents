from dev_agents.runtime.agent import AgentResult, provider_command, run_with_fallback


def test_codex_provider_command_uses_non_interactive_exec() -> None:
    assert provider_command("codex", "fix it") == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        "gpt-5.6-luna",
        "-c",
        'model_reasoning_effort="medium"',
        "fix it",
    ]


def test_codex_provider_command_can_use_luna_high_reasoning() -> None:
    assert 'model_reasoning_effort="high"' in provider_command(
        "codex", "review it", reasoning_effort="high"
    )


def test_claude_provider_command_uses_haiku_medium() -> None:
    assert provider_command("claude", "fix it") == [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--model",
        "haiku",
        "--effort",
        "medium",
        "fix it",
    ]


def test_generic_provider_command_uses_prompt_flag() -> None:
    assert provider_command("other-agent", "fix it") == ["other-agent", "-p", "fix it"]


def test_agy_provider_command_uses_bounded_print_mode() -> None:
    assert provider_command("agy", "fix it", 150) == [
        "agy",
        "--print",
        "fix it",
        "--model=gemini-3.8-flash-low",
        "--effort=low",
        "--dangerously-skip-permissions",
        "--print-timeout=3m0s",
    ]


def test_muse_provider_command_uses_spark() -> None:
    assert provider_command("muse", "fix it") == [
        "muse",
        "exec",
        "--model",
        "muse-spark-1.2",
        "--yolo",
        "fix it",
    ]


def test_run_with_fallback_tries_until_accepted() -> None:
    calls: list[str] = []

    def run(provider: str) -> AgentResult:
        calls.append(provider)
        return AgentResult(1 if provider == "codex" else 0, False)

    result = run_with_fallback(["codex", "claude"], run)
    assert result is not None
    assert result[0] == "claude"
    assert calls == ["codex", "claude"]
