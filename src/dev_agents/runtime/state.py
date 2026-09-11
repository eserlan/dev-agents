"""Atomic, filesystem-backed workflow state."""

from __future__ import annotations

import json
import sqlite3
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


class SqliteStateStore:
    """Transactional workflow state store with JSON documents per namespace."""

    def __init__(self, path: Path, namespace: str, default: dict[str, Any], legacy_path: Path | None = None) -> None:
        self.path = path
        self.namespace = namespace
        self.default = default
        self.legacy_path = legacy_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS state_documents (
                    namespace TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            if connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                connection.execute("INSERT INTO schema_version(version) VALUES (1)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def load(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?", (self.namespace,)
            ).fetchone()
            if row is not None:
                try:
                    value = json.loads(row[0])
                    return value if isinstance(value, dict) else dict(self.default)
                except json.JSONDecodeError:
                    return dict(self.default)
            value = self._load_legacy()
            connection.execute(
                "INSERT OR IGNORE INTO state_documents(namespace, payload) VALUES (?, ?)",
                (self.namespace, json.dumps(value, sort_keys=True)),
            )
            return value

    def _load_legacy(self) -> dict[str, Any]:
        if self.legacy_path is None:
            return dict(self.default)
        try:
            value = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else dict(self.default)
        except (OSError, json.JSONDecodeError):
            return dict(self.default)

    def save(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO state_documents(namespace, payload, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(namespace) DO UPDATE SET
                    payload = excluded.payload, updated_at = CURRENT_TIMESTAMP
                """,
                (self.namespace, payload),
            )
