"""Atomic, filesystem-backed workflow state."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


class JsonStateStore:
    """Persist small workflow state documents without partial writes."""

    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self.default = default

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else dict(self.default)
        except (OSError, json.JSONDecodeError):
            return dict(self.default)

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            temporary = Path(file.name)
        temporary.replace(self.path)
