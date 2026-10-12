"""Bundle repository-local Markdown guidance without following external paths."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_REFERENCES = re.compile(
    r"\[[^\]]*\]\(([^)]+)\)|^\s*\[[^\]]+\]:\s*(\S+)|`([^`\n]+\.(?:md|mdx)(?:#[^`\n]*)?)`",
    re.MULTILINE,
)


def bundle_markdown(repository: Path, entry: Path, *, max_documents: int = 64, max_bytes: int = 256_000) -> str:
    """Inline recursive links and backtick document paths, reporting unavailable guidance.

    Relative references resolve from their document first and then the repository root. Cycles
    are deduplicated; symlinks and traversal outside the repository are never read. Limits are
    explicit in the output rather than silently truncating a linked patterns catalogue.
    """
    root = repository.resolve()
    seen: set[Path] = set()
    missing: set[str] = set()
    sections: list[str] = []
    used = 0

    def visit(path: Path, *, first: bool = False) -> None:
        nonlocal used
        path = path.resolve()
        if not path.is_relative_to(root) or path in seen:
            return
        seen.add(path)
        if not path.is_file():
            missing.add(str(path.relative_to(root)))
            return
        content = path.read_text(encoding="utf-8")
        size = len(content.encode("utf-8"))
        if len(sections) >= max_documents or used + size > max_bytes:
            missing.add(f"{path.relative_to(root)} (bundle limit reached)")
            return
        used += size
        sections.append(content if first else f"\n\n## Linked guidance: {path.relative_to(root)}\n\n{content}")
        for match in _REFERENCES.finditer(content):
            raw = next(value for value in match.groups() if value is not None).strip()
            target = raw[1:raw.index(">")] if raw.startswith("<") and ">" in raw else raw.split()[0]
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            relative = unquote(parsed.path)
            if not relative.lower().endswith((".md", ".mdx")):
                continue
            candidates = [(path.parent / relative).resolve(), (root / relative.lstrip("/")).resolve()]
            candidate = next((p for p in candidates if p.is_relative_to(root) and p.is_file()), None)
            if candidate is not None:
                visit(candidate)
            elif any(p.is_relative_to(root) for p in candidates):
                missing.add(relative)
    visit(entry, first=True)
    if missing:
        sections.append("\n\n## Unavailable linked guidance\n" + "\n".join(f"- {p}" for p in sorted(missing)))
    return "".join(sections)
