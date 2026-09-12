from dev_agents.runtime.agent import AgentResult, provider_command, run_with_fallback


def test_codex_provider_command_uses_non_interactive_exec() -> None:
    assert provider_command("codex", "fix it") == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "fix it",
    ]


def test_generic_provider_command_uses_prompt_flag() -> None:
    assert provider_command("claude", "fix it") == ["claude", "-p", "fix it"]


def test_agy_provider_command_uses_bounded_print_mode() -> None:
    assert provider_command("agy", "fix it", 150) == [
        "agy",
        "--print",
        "fix it",
        "--effort=medium",
        "--dangerously-skip-permissions",
        "--print-timeout=3m0s",
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
