from pathlib import Path

from dev_agents.context.markdown import bundle_markdown


def test_recursively_bundles_relative_root_paths_reference_links_and_cycles(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    skill = root / ".agent/skills/codex-review/SKILL.md"
    general = root / ".agent/skills/code-review/SKILL.md"
    patterns = skill.parent / "references/patterns.md"
    constitution = root / ".specify/memory/constitution.md"
    for path in (skill, general, patterns, constitution):
        path.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("[general](../code-review/SKILL.md#method) and [patterns][p]\n[p]: references/patterns.md\n")
    general.write_text("General methodology. Read `.specify/memory/constitution.md`.")
    patterns.write_text("Full catalogue: race, resource and privacy cases. [cycle](../SKILL.md)")
    constitution.write_text("Privacy contract: no untrusted network requests.")
    result = bundle_markdown(root, skill)
    assert "General methodology" in result
    assert "Full catalogue" in result
    assert "Privacy contract" in result
    assert result.count("Full catalogue") == 1


def test_outside_paths_and_symlinks_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.md").write_text("DO NOT INCLUDE")
    (root / "linked.md").symlink_to(tmp_path / "secret.md")
    entry = root / "SKILL.md"
    entry.write_text("[outside](../secret.md) [symlink](linked.md) [web](https://example.test/guide.md)")
    assert "DO NOT INCLUDE" not in bundle_markdown(root, entry)


def test_unavailable_or_over_budget_guidance_is_reported(tmp_path: Path) -> None:
    entry = tmp_path / "SKILL.md"
    entry.write_text("[missing](missing.md) [large](large.md)")
    (tmp_path / "large.md").write_text("x" * 200)
    bundled = bundle_markdown(tmp_path, entry, max_bytes=100)
    assert "missing.md" in bundled
    assert "large.md (bundle limit reached)" in bundled
