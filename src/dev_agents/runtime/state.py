"""Transactional local state for workflows and webhook workers."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast


class StateError(RuntimeError):
    """The state database could not be used."""


class DuplicateRunError(StateError):
    """A workflow run is already running or has completed."""


@dataclass(frozen=True)
class RunRecord:
    """A persisted workflow run and its latest execution metadata."""

    project_name: str
    workflow: str
    run_id: str
    status: str
    attempt: int
    delivery_id: str | None
    metadata: dict[str, Any]
    error: str | None
    created_at: str
    started_at: str
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True)
class RunClaim:
    """The result of an atomic run claim."""

    claimed: bool
    record: RunRecord


@dataclass(frozen=True)
class RunEvent:
    """A timestamped workflow node or phase transition."""

    project_name: str
    workflow: str
    run_id: str
    sequence: int
    node: str
    event: str
    status: str
    metadata: dict[str, Any]
    started_at: str
    completed_at: str | None


@dataclass(frozen=True)
class PublicationRecord:
    """A compact external-publication receipt linked to a workflow run."""

    project_name: str
    workflow: str
    run_id: str
    channel: str
    destination: str
    page_url: str
    public_url: str | None
    external_id: str | None
    status: str
    error: str | None
    published_at: str
    metadata: dict[str, Any]


class JsonStateStore:
    """Legacy JSON state store retained only for backwards compatibility."""

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


class StateRepository:
    """Relational state repository shared by daemon and workflow workers.

    Agent output is intentionally not stored here. Callers should persist that output in their
    configured log directory and store only metadata and the log path in ``metadata``.
    """

    CURRENT_SCHEMA_VERSION = 4
    BUSY_TIMEOUT_MS = 30_000
    STALE_RUN_SECONDS = 60 * 60

    def __init__(self, path: Path, project_name: str, repository_path: Path | None = None) -> None:
        self.path = path.expanduser()
        self.project_name = project_name
        self.repository_path = (repository_path or Path.cwd()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()
        with self._transaction(immediate=True) as connection:
            now = _timestamp()
            connection.execute(
                """
                INSERT INTO projects(project_name, repository_path, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_name) DO UPDATE SET
                    repository_path = excluded.repository_path,
                    updated_at = excluded.updated_at
                """,
                (self.project_name, str(self.repository_path), now, now),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self, recover: bool = True) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
            version = int(row["version"]) if row and row["version"] is not None else 0
            if version > self.CURRENT_SCHEMA_VERSION:
                raise StateError(
                    f"state database schema {version} is newer than supported "
                    f"version {self.CURRENT_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute("INSERT INTO schema_version(version) VALUES (1)")
                version = 1
            if version < 2:
                self._migrate_to_v2(connection)
                connection.execute("UPDATE schema_version SET version = 2")
                version = 2
            if version < 3:
                self._migrate_to_v3(connection)
                connection.execute("UPDATE schema_version SET version = 3")
                version = 3
            if version < 4:
                self._migrate_to_v4(connection)
                connection.execute("UPDATE schema_version SET version = 4")
            connection.commit()
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.rollback()
            if not recover or not self.path.exists():
                raise
            self._quarantine_corrupt_database()
            self._initialize_database(recover=False)
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_name TEXT PRIMARY KEY,
                repository_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state_documents (
                namespace TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS event_claims (
                namespace TEXT NOT NULL,
                event_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(namespace, event_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                project_name TEXT NOT NULL,
                workflow TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                delivery_id TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_name, workflow, run_id),
                FOREIGN KEY(project_name) REFERENCES projects(project_name)
            );
            CREATE INDEX IF NOT EXISTS runs_status_idx
                ON runs(project_name, workflow, status);
            CREATE TABLE IF NOT EXISTS delivery_claims (
                project_name TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                workflow TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                claimed_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                PRIMARY KEY(project_name, delivery_id),
                FOREIGN KEY(project_name, workflow, run_id)
                    REFERENCES runs(project_name, workflow, run_id)
            );
            CREATE TABLE IF NOT EXISTS feedback_keys (
                project_name TEXT NOT NULL,
                workflow TEXT NOT NULL,
                subject TEXT NOT NULL,
                feedback_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                processed_at TEXT,
                PRIMARY KEY(project_name, workflow, subject, feedback_key)
            );
            CREATE INDEX IF NOT EXISTS feedback_subject_idx
                ON feedback_keys(project_name, workflow, subject, processed_at);
            """
        )

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                project_name TEXT NOT NULL,
                workflow TEXT NOT NULL,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                node TEXT NOT NULL,
                event TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY(project_name, workflow, run_id, sequence),
                FOREIGN KEY(project_name, workflow, run_id)
                    REFERENCES runs(project_name, workflow, run_id)
            );
            CREATE INDEX IF NOT EXISTS run_events_lookup_idx
                ON run_events(project_name, workflow, run_id, sequence);
            """
        )

    @staticmethod
    def _migrate_to_v4(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS publications (
                project_name TEXT NOT NULL,
                workflow TEXT NOT NULL,
                run_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                destination TEXT NOT NULL DEFAULT '',
                page_url TEXT NOT NULL DEFAULT '',
                public_url TEXT,
                external_id TEXT,
                status TEXT NOT NULL,
                error TEXT,
                published_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(
                    project_name, workflow, run_id, channel, destination, page_url
                ),
                FOREIGN KEY(project_name, workflow, run_id)
                    REFERENCES runs(project_name, workflow, run_id)
            );
            CREATE INDEX IF NOT EXISTS publications_lookup_idx
                ON publications(project_name, workflow, run_id, published_at);
            """
        )

    def _quarantine_corrupt_database(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{suffix}")
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if candidate.exists():
                target = backup if candidate == self.path else backup.with_name(backup.name + candidate.suffix)
                candidate.replace(target)
        return backup

    @staticmethod
    def _select_run(
        connection: sqlite3.Connection,
        project_name: str,
        workflow: str,
        run_id: str,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM runs WHERE project_name = ? AND workflow = ? AND run_id = ?",
                (project_name, workflow, run_id),
            ).fetchone(),
        )

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return RunRecord(
            project_name=str(row["project_name"]),
            workflow=str(row["workflow"]),
            run_id=str(row["run_id"]),
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            delivery_id=str(row["delivery_id"]) if row["delivery_id"] is not None else None,
            metadata=metadata if isinstance(metadata, dict) else {},
            error=str(row["error"]) if row["error"] is not None else None,
            created_at=str(row["created_at"]),
            started_at=str(row["started_at"]),
            completed_at=(str(row["completed_at"]) if row["completed_at"] is not None else None),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _run_event(row: sqlite3.Row) -> RunEvent:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return RunEvent(
            project_name=str(row["project_name"]),
            workflow=str(row["workflow"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            node=str(row["node"]),
            event=str(row["event"]),
            status=str(row["status"]),
            metadata=metadata if isinstance(metadata, dict) else {},
            started_at=str(row["started_at"]),
            completed_at=(str(row["completed_at"]) if row["completed_at"] is not None else None),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        project_name: str,
        workflow: str,
        run_id: str,
        node: str,
        event: str,
        status: str,
        metadata: dict[str, Any] | None,
        started_at: str,
        completed_at: str | None,
    ) -> None:
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM run_events
            WHERE project_name = ? AND workflow = ? AND run_id = ?
            """,
            (project_name, workflow, run_id),
        ).fetchone()["sequence"]
        connection.execute(
            """
            INSERT INTO run_events(
                project_name, workflow, run_id, sequence, node, event, status,
                metadata, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_name,
                workflow,
                run_id,
                sequence,
                node,
                event,
                status,
                json.dumps(metadata or {}, sort_keys=True),
                started_at,
                completed_at,
            ),
        )

    def get_run(self, workflow: str, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = self._select_run(connection, self.project_name, workflow, run_id)
        return self._run_record(row) if row is not None else None

    def load_run(self, workflow: str, run_id: str) -> RunRecord | None:
        """Load one run record."""
        return self.get_run(workflow, run_id)

    def list_runs(self, workflow: str | None = None, limit: int = 100) -> list[RunRecord]:
        """Load recent runs for the configured project."""
        query = "SELECT * FROM runs WHERE project_name = ?"
        parameters: list[Any] = [self.project_name]
        if workflow is not None:
            query += " AND workflow = ?"
            parameters.append(workflow)
        query += " ORDER BY started_at DESC LIMIT ?"
        parameters.append(max(1, limit))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._run_record(row) for row in rows]

    def list_run_events(self, workflow: str, run_id: str) -> list[RunEvent]:
        """Load the ordered timeline for one run."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                ORDER BY sequence
                """,
                (self.project_name, workflow, run_id),
            ).fetchall()
        return [self._run_event(row) for row in rows]

    @staticmethod
    def _publication_record(row: sqlite3.Row) -> PublicationRecord:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return PublicationRecord(
            project_name=str(row["project_name"]),
            workflow=str(row["workflow"]),
            run_id=str(row["run_id"]),
            channel=str(row["channel"]),
            destination=str(row["destination"]),
            page_url=str(row["page_url"]),
            public_url=str(row["public_url"]) if row["public_url"] is not None else None,
            external_id=str(row["external_id"]) if row["external_id"] is not None else None,
            status=str(row["status"]),
            error=str(row["error"]) if row["error"] is not None else None,
            published_at=str(row["published_at"]),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def list_run_publications(self, workflow: str, run_id: str) -> list[PublicationRecord]:
        """Load external-publication receipts for one workflow run."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM publications
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                ORDER BY published_at, channel, destination, page_url
                """,
                (self.project_name, workflow, run_id),
            ).fetchall()
        return [self._publication_record(row) for row in rows]

    def list_publications(
        self,
        workflow: str = "release-comms",
        *,
        channel: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[PublicationRecord]:
        """Query publications across runs filtered by workflow, channel, or status."""
        query = "SELECT * FROM publications WHERE project_name = ? AND workflow = ?"
        params: list[Any] = [self.project_name, workflow]
        if channel is not None:
            query += " AND channel = ?"
            params.append(channel)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY published_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._publication_record(row) for row in rows]

    def record_publication(
        self,
        workflow: str,
        run_id: str,
        channel: str,
        *,
        destination: str | None = None,
        page_url: str | None = None,
        public_url: str | None = None,
        external_id: str | None = None,
        status: str = "published",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublicationRecord:
        """Upsert a compact external-publication receipt."""
        now = _timestamp()
        destination_value = destination or ""
        page_url_value = page_url or ""
        with self._transaction(immediate=True) as connection:
            run = self._select_run(connection, self.project_name, workflow, run_id)
            if run is None:
                raise StateError(f"unknown run {workflow}/{run_id}")
            connection.execute(
                """
                INSERT INTO publications(
                    project_name, workflow, run_id, channel, destination, page_url,
                    public_url, external_id, status, error, published_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_name, workflow, run_id, channel, destination, page_url)
                DO UPDATE SET
                    public_url = excluded.public_url,
                    external_id = excluded.external_id,
                    status = excluded.status,
                    error = excluded.error,
                    published_at = excluded.published_at,
                    metadata = excluded.metadata
                """,
                (
                    self.project_name,
                    workflow,
                    run_id,
                    channel,
                    destination_value,
                    page_url_value,
                    public_url,
                    external_id,
                    status,
                    error,
                    now,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM publications
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                  AND channel = ? AND destination = ? AND page_url = ?
                """,
                (
                    self.project_name,
                    workflow,
                    run_id,
                    channel,
                    destination_value,
                    page_url_value,
                ),
            ).fetchone()
            if row is None:
                raise StateError("publication disappeared while being recorded")
            return self._publication_record(row)

    def record_event(
        self,
        workflow: str,
        run_id: str,
        node: str,
        event: str,
        *,
        status: str = "completed",
        metadata: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Append a compact, timestamped node event to a run timeline."""
        now = _timestamp()
        with self._transaction(immediate=True) as connection:
            row = self._select_run(connection, self.project_name, workflow, run_id)
            if row is None:
                raise StateError(f"unknown run {workflow}/{run_id}")
            self._insert_event(
                connection,
                self.project_name,
                workflow,
                run_id,
                node,
                event,
                status,
                metadata,
                now,
                now,
            )
            event_row = connection.execute(
                """
                SELECT * FROM run_events
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (self.project_name, workflow, run_id),
            ).fetchone()
            if event_row is None:
                raise StateError("event disappeared while being recorded")
            return self._run_event(event_row)

    def update_run(
        self,
        workflow: str,
        run_id: str,
        *,
        status: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Update non-terminal run metadata without changing its attempt."""
        now = _timestamp()
        with self._transaction(immediate=True) as connection:
            row = self._select_run(connection, self.project_name, workflow, run_id)
            if row is None:
                raise StateError(f"unknown run {workflow}/{run_id}")
            try:
                old_metadata = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError):
                old_metadata = {}
            merged_metadata = old_metadata if isinstance(old_metadata, dict) else {}
            if metadata:
                merged_metadata.update(metadata)
            next_status = status or str(row["status"])
            connection.execute(
                """
                UPDATE runs SET status = ?, error = ?, metadata = ?, updated_at = ?
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                """,
                (
                    next_status,
                    error,
                    json.dumps(merged_metadata, sort_keys=True),
                    now,
                    self.project_name,
                    workflow,
                    run_id,
                ),
            )
            updated = self._select_run(connection, self.project_name, workflow, run_id)
            if updated is None:
                raise StateError("run disappeared while being updated")
            return self._run_record(updated)

    def claim_run(
        self,
        workflow: str,
        run_id: str,
        *,
        delivery_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunClaim:
        """Atomically claim a run; failed or stale runs may be retried."""
        now = _timestamp()
        payload = json.dumps(metadata or {}, sort_keys=True)
        with self._transaction(immediate=True) as connection:
            existing_delivery = None
            if delivery_id is not None:
                existing_delivery = connection.execute(
                    """
                    SELECT * FROM delivery_claims
                    WHERE project_name = ? AND delivery_id = ?
                    """,
                    (self.project_name, delivery_id),
                ).fetchone()
                if existing_delivery is not None:
                    row = self._select_run(
                        connection,
                        self.project_name,
                        str(existing_delivery["workflow"]),
                        str(existing_delivery["run_id"]),
                    )
                    if row is None:
                        raise StateError("delivery claim references a missing run")
                    delivery_is_stale = (
                        row["status"] == "running"
                        and (datetime.now(UTC) - _parse_timestamp(str(row["started_at"]))).total_seconds()
                        >= self.STALE_RUN_SECONDS
                    )
                    if row["status"] == "completed" or (
                        existing_delivery["status"] == "running" and not delivery_is_stale
                    ):
                        return RunClaim(False, self._run_record(row))

            row = self._select_run(connection, self.project_name, workflow, run_id)
            if row is not None and row["status"] == "completed":
                return RunClaim(False, self._run_record(row))
            if row is not None and row["status"] == "running":
                started = _parse_timestamp(str(row["started_at"]))
                if (datetime.now(UTC) - started).total_seconds() < self.STALE_RUN_SECONDS:
                    return RunClaim(False, self._run_record(row))

            if row is None:
                connection.execute(
                    """
                    INSERT INTO runs(
                        project_name, workflow, run_id, status, attempt, delivery_id,
                        metadata, created_at, started_at, updated_at
                    ) VALUES (?, ?, ?, 'running', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.project_name,
                        workflow,
                        run_id,
                        delivery_id,
                        payload,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE runs SET
                        status = 'running', attempt = attempt + 1,
                        delivery_id = COALESCE(?, delivery_id), metadata = ?, error = NULL,
                        completed_at = NULL, started_at = ?, updated_at = ?
                    WHERE project_name = ? AND workflow = ? AND run_id = ?
                    """,
                    (
                        delivery_id,
                        payload,
                        now,
                        now,
                        self.project_name,
                        workflow,
                        run_id,
                    ),
                )

            if delivery_id is not None:
                if existing_delivery is None:
                    connection.execute(
                        """
                        INSERT INTO delivery_claims(
                            project_name, delivery_id, workflow, run_id, status, claimed_at
                        ) VALUES (?, ?, ?, ?, 'running', ?)
                        """,
                        (self.project_name, delivery_id, workflow, run_id, now),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE delivery_claims SET
                            status = 'running', attempt = attempt + 1,
                            workflow = ?, run_id = ?, claimed_at = ?, completed_at = NULL, error = NULL
                        WHERE project_name = ? AND delivery_id = ?
                        """,
                        (workflow, run_id, now, self.project_name, delivery_id),
                    )
            self._insert_event(
                connection,
                self.project_name,
                workflow,
                run_id,
                "__run__",
                "run_started",
                "running",
                {"attempt": (int(row["attempt"]) + 1 if row is not None else 1)},
                now,
                None,
            )
            claimed = self._select_run(connection, self.project_name, workflow, run_id)
            if claimed is None:
                raise StateError("run disappeared while being claimed")
            return RunClaim(True, self._run_record(claimed))

    def complete_run(
        self,
        workflow: str,
        run_id: str,
        *,
        status: str = "completed",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        """Commit the terminal status of a run and its delivery claim atomically."""
        if status not in {"completed", "failed"}:
            raise ValueError("terminal run status must be completed or failed")
        now = _timestamp()
        with self._transaction(immediate=True) as connection:
            row = self._select_run(connection, self.project_name, workflow, run_id)
            if row is None:
                raise StateError(f"unknown run {workflow}/{run_id}")
            try:
                old_metadata = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError):
                old_metadata = {}
            merged_metadata = old_metadata if isinstance(old_metadata, dict) else {}
            if metadata:
                merged_metadata.update(metadata)
            connection.execute(
                """
                UPDATE runs SET status = ?, error = ?, metadata = ?, completed_at = ?, updated_at = ?
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                """,
                (
                    status,
                    error,
                    json.dumps(merged_metadata, sort_keys=True),
                    now,
                    now,
                    self.project_name,
                    workflow,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE delivery_claims SET status = ?, completed_at = ?, error = ?
                WHERE project_name = ? AND workflow = ? AND run_id = ?
                """,
                (status, now, error, self.project_name, workflow, run_id),
            )
            self._insert_event(
                connection,
                self.project_name,
                workflow,
                run_id,
                "__run__",
                "run_completed",
                status,
                {"error": error} if error else None,
                now,
                now,
            )
            updated = self._select_run(connection, self.project_name, workflow, run_id)
            if updated is None:
                raise StateError("run disappeared while being completed")
            return self._run_record(updated)

    def unprocessed_feedback(self, workflow: str, subject: str, keys: list[str]) -> list[str]:
        """Record observed feedback and return keys not yet marked processed."""
        now = _timestamp()
        with self._transaction(immediate=True) as connection:
            for key in sorted(set(keys)):
                connection.execute(
                    """
                    INSERT INTO feedback_keys(
                        project_name, workflow, subject, feedback_key, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_name, workflow, subject, feedback_key)
                    DO UPDATE SET last_seen_at = excluded.last_seen_at
                    """,
                    (self.project_name, workflow, subject, key, now, now),
                )
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            rows = connection.execute(
                f"""SELECT feedback_key FROM feedback_keys
                    WHERE project_name = ? AND workflow = ? AND subject = ?
                    AND feedback_key IN ({placeholders}) AND processed_at IS NULL
                    ORDER BY feedback_key""",
                (self.project_name, workflow, subject, *keys),
            ).fetchall()
        return [str(row["feedback_key"]) for row in rows]

    def mark_feedback_processed(self, workflow: str, subject: str, keys: list[str]) -> None:
        """Mark feedback identities handled by a successful workflow run."""
        if not keys:
            return
        now = _timestamp()
        with self._transaction(immediate=True) as connection:
            for key in sorted(set(keys)):
                connection.execute(
                    """
                    INSERT INTO feedback_keys(
                        project_name, workflow, subject, feedback_key,
                        first_seen_at, last_seen_at, processed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_name, workflow, subject, feedback_key)
                    DO UPDATE SET last_seen_at = excluded.last_seen_at, processed_at = excluded.processed_at
                    """,
                    (self.project_name, workflow, subject, key, now, now, now),
                )

    def load_document(
        self, namespace: str, default: dict[str, Any], legacy_path: Path | None = None
    ) -> dict[str, Any]:
        """Compatibility API for small legacy documents during migration."""
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload FROM state_documents WHERE namespace = ?", (namespace,)
            ).fetchone()
            if row is not None:
                try:
                    value = json.loads(row["payload"])
                except json.JSONDecodeError:
                    value = dict(default)
                return value if isinstance(value, dict) else dict(default)
            value = dict(default)
            if legacy_path is not None:
                try:
                    candidate = json.loads(legacy_path.read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        value = candidate
                except (OSError, json.JSONDecodeError):
                    pass
            connection.execute(
                "INSERT INTO state_documents(namespace, payload) VALUES (?, ?)",
                (namespace, json.dumps(value, sort_keys=True)),
            )
            return value

    def save_document(self, namespace: str, value: dict[str, Any]) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True)
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO state_documents(namespace, payload, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(namespace) DO UPDATE SET
                    payload = excluded.payload, updated_at = CURRENT_TIMESTAMP
                """,
                (namespace, payload),
            )


class SqliteStateStore:
    """Compatibility facade for the pre-issue generic store API."""

    def __init__(
        self,
        path: Path,
        namespace: str,
        default: dict[str, Any],
        legacy_path: Path | None = None,
    ) -> None:
        self.namespace = namespace
        self.default = default
        self.legacy_path = legacy_path
        self.repository = StateRepository(path, namespace)

    def load(self) -> dict[str, Any]:
        return self.repository.load_document(self.namespace, self.default, self.legacy_path)

    def save(self, value: dict[str, Any]) -> None:
        self.repository.save_document(self.namespace, value)

    def claim_once(self, event_id: str) -> bool:
        return self.repository.claim_run("event", event_id).claimed


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def state_database_path(configured: Path | None, default: Path) -> Path:
    """Resolve a configured SQLite path, accepting a legacy ``.json`` setting."""
    path = (configured or default).expanduser()
    return path.with_suffix(".db") if path.suffix.lower() == ".json" else path


def legacy_json_path(configured: Path | None, database_path: Path) -> Path:
    """Return the legacy JSON location associated with a state database."""
    if configured is not None and configured.suffix.lower() == ".json":
        return configured.expanduser()
    return database_path.with_suffix(".json")
