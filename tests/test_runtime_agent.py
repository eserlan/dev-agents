from dev_agents.runtime.agent import provider_command


def test_codex_provider_command_uses_non_interactive_exec() -> None:
    assert provider_command("codex", "fix it") == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "fix it",
    ]


def test_generic_provider_command_uses_prompt_flag() -> None:
    assert provider_command("claude", "fix it") == ["claude", "-p", "fix it"]
