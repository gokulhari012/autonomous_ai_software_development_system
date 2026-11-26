"""SQLite persistent state for the autonomous development controller.

* Versioned schema migrations (``schema_version`` table).
* WAL mode + busy timeout so the dashboard and Telegram bot can read while the
  orchestrator writes.
* Every task transition is written in a single ``BEGIN IMMEDIATE`` transaction
  together with its history row, so a crash can never leave a half-applied
  transition.
* Task leases and named locks prevent duplicate execution.

Nothing in the controller relies on in-memory state: everything needed to
resume after a crash is stored here.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

JSON_COLUMNS = {
    "acceptance_criteria",
    "related_modules",
    "test_cases",
    "validation_evidence",
    "file_scope",
    "design",
    "metadata",
    "detail",
    "details",
    "args",
    "scan_summary",
    "output_json",
    "auditor_assessment",
    "app_types",
}

TERMINAL_TASK_STATUSES = {"COMPLETED", "CANCELLED"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(*parts: Any) -> str:
    normalized = "|".join(" ".join(str(p).lower().split()) for p in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            workspace_path TEXT,
            mode TEXT,
            app_types TEXT,
            config_path TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            current_stage TEXT,
            scan_summary TEXT,
            base_branch TEXT,
            dev_branch TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            stop_reason TEXT,
            tasks_completed INTEGER NOT NULL DEFAULT 0,
            tasks_blocked INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            pid INTEGER,
            hostname TEXT
        );

        CREATE TABLE requirements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 3,
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            related_modules TEXT NOT NULL DEFAULT '[]',
            test_cases TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'PROPOSED',
            source TEXT NOT NULL DEFAULT 'inferred',
            mandatory INTEGER NOT NULL DEFAULT 1,
            validation_evidence TEXT,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(project_id, code),
            UNIQUE(project_id, fingerprint)
        );

        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            requirement_id INTEGER REFERENCES requirements(id),
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'feature',
            priority INTEGER NOT NULL DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'PENDING',
            stage TEXT NOT NULL DEFAULT 'ARCHITECTURE_DESIGN',
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            stage_error_count INTEGER NOT NULL DEFAULT 0,
            agent_turns_used INTEGER NOT NULL DEFAULT 0,
            assigned_agent TEXT,
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            file_scope TEXT NOT NULL DEFAULT '[]',
            design TEXT,
            base_commit TEXT,
            blocked_reason TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at REAL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            UNIQUE(project_id, fingerprint)
        );
        CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);

        CREATE TABLE task_dependencies (
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id),
            PRIMARY KEY (task_id, depends_on_task_id)
        );

        CREATE TABLE task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            from_stage TEXT,
            to_stage TEXT,
            from_status TEXT,
            to_status TEXT,
            reason TEXT,
            actor TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE stage_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            stage TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            detail TEXT
        );

        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            agent_name TEXT NOT NULL,
            stage TEXT,
            backend TEXT NOT NULL DEFAULT 'claude',
            status TEXT NOT NULL,
            input_summary TEXT,
            output_summary TEXT,
            output_json TEXT,
            error_message TEXT,
            session_id TEXT,
            model TEXT,
            num_turns INTEGER,
            tool_calls INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER,
            cost_usd REAL,
            duration_ms INTEGER,
            attempt INTEGER NOT NULL DEFAULT 1,
            run_id INTEGER,
            last_activity_at TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX idx_agent_runs_task ON agent_runs(task_id);

        CREATE TABLE issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            title TEXT NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            file_path TEXT,
            line_number INTEGER,
            description TEXT NOT NULL DEFAULT '',
            recommendation TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            fix_attempts INTEGER NOT NULL DEFAULT 0,
            fix_summary TEXT,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE INDEX idx_issues_task ON issues(task_id, status);

        CREATE TABLE checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            git_commit TEXT NOT NULL,
            branch TEXT,
            description TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE test_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            stage TEXT,
            test_type TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            passed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            exit_code INTEGER,
            duration_seconds REAL,
            command TEXT,
            summary TEXT,
            report_path TEXT,
            junit_path TEXT,
            artifacts_dir TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE test_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_run_id INTEGER NOT NULL REFERENCES test_runs(id),
            nodeid TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_seconds REAL,
            message TEXT
        );
        CREATE INDEX idx_test_cases_run ON test_cases(test_run_id);

        CREATE TABLE security_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER,
            findings_count INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            error TEXT,
            command TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE security_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            scan_id INTEGER,
            tool_name TEXT NOT NULL,
            rule_id TEXT,
            severity TEXT NOT NULL,
            confidence TEXT,
            file_path TEXT,
            line_number INTEGER,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            baseline INTEGER NOT NULL DEFAULT 0,
            auditor_assessment TEXT,
            fingerprint TEXT NOT NULL,
            report_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(project_id, fingerprint)
        );

        CREATE TABLE deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id INTEGER,
            environment TEXT NOT NULL DEFAULT 'staging',
            method TEXT NOT NULL,
            status TEXT NOT NULL,
            url TEXT,
            pid INTEGER,
            pid_create_time REAL,
            health_status TEXT,
            log_path TEXT,
            detail TEXT,
            started_at TEXT NOT NULL,
            stopped_at TEXT
        );

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            dedupe_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE INDEX idx_notifications_status ON notifications(status);

        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            project_id TEXT,
            task_id INTEGER,
            agent_name TEXT,
            event_type TEXT NOT NULL,
            status TEXT,
            summary TEXT,
            metadata TEXT,
            source TEXT NOT NULL DEFAULT 'orchestrator',
            actual INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX idx_events_project ON events(project_id, id);

        CREATE TABLE control_flags (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE control_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            command TEXT NOT NULL,
            args TEXT,
            requested_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            result TEXT,
            created_at TEXT NOT NULL,
            applied_at TEXT
        );

        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            details TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            decided_by TEXT,
            decision_note TEXT,
            requested_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE locks (
            name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            pid INTEGER,
            hostname TEXT,
            acquired_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );

        CREATE TABLE file_reservations (
            project_id TEXT NOT NULL,
            path TEXT NOT NULL,
            task_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, path)
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            allowed INTEGER NOT NULL DEFAULT 1,
            detail TEXT
        );

        CREATE TABLE kv (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in JSON_COLUMNS.intersection(result):
        value = result[key]
        if isinstance(value, str) and value:
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return result


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bool):
        return int(value)
    return value


class StateManager:
    """Thread-safe access to the controller database (one connection per thread)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.hostname = socket.gethostname()
        self.migrate()

    # ------------------------------------------------------------------ core
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        if conn.in_transaction:  # nested use joins the outer transaction
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        rows = self._conn().execute(sql, tuple(params)).fetchall()
        return [_decode_row(r) for r in rows]  # type: ignore[misc]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        return _decode_row(self._conn().execute(sql, tuple(params)).fetchone())

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.transaction() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.lastrowid or cur.rowcount

    def _insert(self, conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cur = conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(_encode(v) for v in values.values()),
        )
        return int(cur.lastrowid)

    def _update(self, conn: sqlite3.Connection, table: str, key: str, key_value: Any, values: dict[str, Any]) -> None:
        if not values:
            return
        assignments = ", ".join(f"{col} = ?" for col in values)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {key} = ?",
            tuple(_encode(v) for v in values.values()) + (key_value,),
        )

    # ------------------------------------------------------------ migrations
    def migrate(self) -> int:
        conn = self._conn()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
        current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in [s.strip() for s in script.split(";") if s.strip()]:
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, utcnow()))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            current = version
        return current

    def schema_version(self) -> int:
        row = self._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)

    # -------------------------------------------------------------- projects
    def upsert_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        now = utcnow()
        with self.transaction() as conn:
            existing = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if existing:
                fields["updated_at"] = now
                self._update(conn, "projects", "id", project_id, fields)
            else:
                values = {"id": project_id, "created_at": now, "updated_at": now, **fields}
                values.setdefault("name", project_id)
                values.setdefault("path", "")
                self._insert(conn, "projects", values)
        return self.get_project(project_id)  # type: ignore[return-value]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def list_projects(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM projects ORDER BY updated_at DESC")

    def update_project(self, project_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.transaction() as conn:
            self._update(conn, "projects", "id", project_id, fields)

    # ------------------------------------------------------------------ runs
    def start_run(self, project_id: str, pid: int) -> int:
        with self.transaction() as conn:
            return self._insert(
                conn,
                "runs",
                {"project_id": project_id, "status": "RUNNING", "started_at": utcnow(), "pid": pid, "hostname": self.hostname},
            )

    def finish_run(self, run_id: int, status: str, stop_reason: str) -> None:
        with self.transaction() as conn:
            self._update(conn, "runs", "id", run_id, {"status": status, "stop_reason": stop_reason, "finished_at": utcnow()})

    def increment_run(self, run_id: int, field: str, amount: float = 1) -> None:
        if field not in {"tasks_completed", "tasks_blocked", "cost_usd"}:
            raise ValueError(field)
        self.execute(f"UPDATE runs SET {field} = {field} + ? WHERE id = ?", (amount, run_id))

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def mark_stale_runs(self, project_id: str, reason: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE runs SET status = 'INTERRUPTED', stop_reason = ?, finished_at = ? "
                "WHERE project_id = ? AND status = 'RUNNING'",
                (reason, utcnow(), project_id),
            )
            return cur.rowcount

    # ---------------------------------------------------------- requirements
    def next_requirement_code(self, project_id: str) -> str:
        row = self.query_one("SELECT COUNT(*) AS n FROM requirements WHERE project_id = ?", (project_id,))
        return f"REQ-{(row['n'] if row else 0) + 1:03d}"

    def add_requirement(
        self,
        project_id: str,
        title: str,
        description: str,
        priority: int = 3,
        acceptance_criteria: list[str] | None = None,
        related_modules: list[str] | None = None,
        test_cases: list[str] | None = None,
        source: str = "inferred",
        status: str = "PROPOSED",
        mandatory: bool = True,
    ) -> tuple[int, bool]:
        """Insert a requirement unless an equivalent one exists. Returns (id, created)."""
        fp = fingerprint(title, description)
        now = utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM requirements WHERE project_id = ? AND (fingerprint = ? OR lower(title) = lower(?))",
                (project_id, fp, title.strip()),
            ).fetchone()
            if row:
                return int(row["id"]), False
            count = conn.execute("SELECT COUNT(*) FROM requirements WHERE project_id = ?", (project_id,)).fetchone()[0]
            req_id = self._insert(
                conn,
                "requirements",
                {
                    "project_id": project_id,
                    "code": f"REQ-{count + 1:03d}",
                    "title": title.strip(),
                    "description": description.strip(),
                    "priority": priority,
                    "acceptance_criteria": acceptance_criteria or [],
                    "related_modules": related_modules or [],
                    "test_cases": test_cases or [],
                    "source": source,
                    "status": status,
                    "mandatory": mandatory,
                    "fingerprint": fp,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return req_id, True

    def get_requirement(self, req_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM requirements WHERE id = ?", (req_id,))

    def get_requirement_by_code(self, project_id: str, code: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM requirements WHERE project_id = ? AND code = ?", (project_id, code))

    def list_requirements(self, project_id: str, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            statuses = list(statuses)
            marks = ",".join("?" for _ in statuses)
            return self.query(
                f"SELECT * FROM requirements WHERE project_id = ? AND status IN ({marks}) ORDER BY priority, id",
                (project_id, *statuses),
            )
        return self.query("SELECT * FROM requirements WHERE project_id = ? ORDER BY priority, id", (project_id,))

    def update_requirement(self, req_id: int, **fields: Any) -> None:
        if fields.get("status") == "COMPLETED" and not fields.get("validation_evidence"):
            existing = self.get_requirement(req_id)
            if not existing or not existing.get("validation_evidence"):
                raise ValueError("A requirement cannot be marked COMPLETED without validation evidence.")
        fields["updated_at"] = utcnow()
        if fields.get("status") == "COMPLETED":
            fields["completed_at"] = utcnow()
        with self.transaction() as conn:
            self._update(conn, "requirements", "id", req_id, fields)

    # ----------------------------------------------------------------- tasks
    def create_task(
        self,
        project_id: str,
        title: str,
        description: str = "",
        requirement_id: int | None = None,
        priority: int = 3,
        kind: str = "feature",
        acceptance_criteria: list[str] | None = None,
        file_scope: list[str] | None = None,
        max_retries: int = 3,
        stage: str = "ARCHITECTURE_DESIGN",
    ) -> tuple[int, bool]:
        fp = fingerprint(requirement_id or "", title)
        now = utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE project_id = ? AND fingerprint = ?", (project_id, fp)
            ).fetchone()
            if row:
                return int(row["id"]), False
            task_id = self._insert(
                conn,
                "tasks",
                {
                    "project_id": project_id,
                    "requirement_id": requirement_id,
                    "title": title.strip(),
                    "description": description.strip(),
                    "kind": kind,
                    "priority": priority,
                    "status": "PENDING",
                    "stage": stage,
                    "max_retries": max_retries,
                    "acceptance_criteria": acceptance_criteria or [],
                    "file_scope": file_scope or [],
                    "fingerprint": fp,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.execute(
                "INSERT INTO task_history (task_id, to_stage, to_status, reason, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, stage, "PENDING", "created", "orchestrator", now),
            )
            return task_id, True

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        if task_id == depends_on_task_id:
            return
        self.execute(
            "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (task_id, depends_on_task_id),
        )

    def task_dependencies(self, task_id: int) -> list[int]:
        rows = self.query("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?", (task_id,))
        return [r["depends_on_task_id"] for r in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def list_tasks(self, project_id: str, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            statuses = list(statuses)
            marks = ",".join("?" for _ in statuses)
            return self.query(
                f"SELECT * FROM tasks WHERE project_id = ? AND status IN ({marks}) ORDER BY priority, id",
                (project_id, *statuses),
            )
        return self.query("SELECT * FROM tasks WHERE project_id = ? ORDER BY priority, id", (project_id,))

    def update_task_fields(self, task_id: int, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.transaction() as conn:
            self._update(conn, "tasks", "id", task_id, fields)

    def transition_task(
        self,
        task_id: int,
        *,
        to_stage: str | None = None,
        to_status: str | None = None,
        reason: str = "",
        actor: str = "orchestrator",
        **fields: Any,
    ) -> dict[str, Any]:
        """Atomically move a task to a new stage/status and record the history row."""
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute("SELECT stage, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(f"task {task_id} not found")
            if current["status"] == "COMPLETED" and to_status not in (None, "COMPLETED"):
                raise ValueError(f"task {task_id} is COMPLETED and cannot be reopened by a transition")
            updates: dict[str, Any] = dict(fields)
            if to_stage:
                updates["stage"] = to_stage
            if to_status:
                updates["status"] = to_status
                if to_status == "COMPLETED":
                    updates["completed_at"] = now
                if to_status == "IN_PROGRESS":
                    updates.setdefault("started_at", now)
            updates["updated_at"] = now
            self._update(conn, "tasks", "id", task_id, updates)
            conn.execute(
                "INSERT INTO task_history (task_id, from_stage, to_stage, from_status, to_status, reason, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, current["stage"], to_stage or current["stage"], current["status"], to_status or current["status"], reason[:2000], actor, now),
            )
        return self.get_task(task_id)  # type: ignore[return-value]

    def task_history(self, task_id: int) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM task_history WHERE task_id = ? ORDER BY id", (task_id,))

    def runnable_tasks(self, project_id: str, owner: str) -> list[dict[str, Any]]:
        """Tasks that can be picked up now: pending or resumable, dependencies complete, not leased by others."""
        now = time.time()
        rows = self.query(
            """
            SELECT t.* FROM tasks t
            WHERE t.project_id = ?
              AND t.status IN ('PENDING', 'IN_PROGRESS')
              AND t.cancel_requested = 0
              AND (t.lease_owner IS NULL OR t.lease_owner = ? OR t.lease_expires_at < ?)
              AND NOT EXISTS (
                  SELECT 1 FROM task_dependencies d JOIN tasks dep ON dep.id = d.depends_on_task_id
                  WHERE d.task_id = t.id AND dep.status != 'COMPLETED'
              )
            ORDER BY CASE t.status WHEN 'IN_PROGRESS' THEN 0 ELSE 1 END, t.priority, t.id
            """,
            (project_id, owner, now),
        )
        return rows

    def acquire_task_lease(self, task_id: int, owner: str, seconds: int) -> bool:
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET lease_owner = ?, lease_expires_at = ? "
                "WHERE id = ? AND (lease_owner IS NULL OR lease_owner = ? OR lease_expires_at < ?)",
                (owner, now + seconds, task_id, owner, now),
            )
            return cur.rowcount == 1

    def renew_task_lease(self, task_id: int, owner: str, seconds: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET lease_expires_at = ? WHERE id = ? AND lease_owner = ?",
                (time.time() + seconds, task_id, owner),
            )
            return cur.rowcount == 1

    def release_task_lease(self, task_id: int, owner: str) -> None:
        self.execute(
            "UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ? AND lease_owner = ?",
            (task_id, owner),
        )

    def release_stale_leases(self, project_id: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL "
                "WHERE project_id = ? AND lease_owner IS NOT NULL AND lease_expires_at < ?",
                (project_id, time.time()),
            )
            return cur.rowcount

    # ------------------------------------------------------------ stage runs
    def start_stage_run(self, project_id: str, task_id: int | None, stage: str, attempt: int = 1) -> int:
        with self.transaction() as conn:
            return self._insert(
                conn,
                "stage_runs",
                {"project_id": project_id, "task_id": task_id, "stage": stage, "attempt": attempt, "status": "STARTED", "started_at": utcnow()},
            )

    def finish_stage_run(self, stage_run_id: int, status: str, detail: dict[str, Any] | None = None) -> None:
        with self.transaction() as conn:
            self._update(conn, "stage_runs", "id", stage_run_id, {"status": status, "finished_at": utcnow(), "detail": detail or {}})

    def last_stage_run(self, project_id: str, task_id: int | None, stage: str) -> dict[str, Any] | None:
        if task_id is None:
            return self.query_one(
                "SELECT * FROM stage_runs WHERE project_id = ? AND task_id IS NULL AND stage = ? ORDER BY id DESC LIMIT 1",
                (project_id, stage),
            )
        return self.query_one(
            "SELECT * FROM stage_runs WHERE project_id = ? AND task_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
            (project_id, task_id, stage),
        )

    def count_stage_runs(self, task_id: int, stage: str) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM stage_runs WHERE task_id = ? AND stage = ?", (task_id, stage))
        return int(row["n"]) if row else 0

    def interrupt_open_stage_runs(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM stage_runs WHERE project_id = ? AND status = 'STARTED'", (project_id,))
        with self.transaction() as conn:
            conn.execute(
                "UPDATE stage_runs SET status = 'INTERRUPTED', finished_at = ? WHERE project_id = ? AND status = 'STARTED'",
                (utcnow(), project_id),
            )
        return rows

    # ------------------------------------------------------------ agent runs
    def start_agent_run(self, project_id: str, task_id: int | None, agent_name: str, stage: str | None, backend: str, input_summary: str, attempt: int, run_id: int | None) -> int:
        now = utcnow()
        with self.transaction() as conn:
            return self._insert(
                conn,
                "agent_runs",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "agent_name": agent_name,
                    "stage": stage,
                    "backend": backend,
                    "status": "RUNNING",
                    "input_summary": input_summary[:4000],
                    "attempt": attempt,
                    "run_id": run_id,
                    "started_at": now,
                    "last_activity_at": now,
                },
            )

    def touch_agent_run(self, agent_run_id: int, tool_calls_delta: int = 0) -> None:
        self.execute(
            "UPDATE agent_runs SET last_activity_at = ?, tool_calls = tool_calls + ? WHERE id = ?",
            (utcnow(), tool_calls_delta, agent_run_id),
        )

    def finish_agent_run(self, agent_run_id: int, **fields: Any) -> None:
        fields["finished_at"] = utcnow()
        with self.transaction() as conn:
            self._update(conn, "agent_runs", "id", agent_run_id, fields)

    def interrupt_running_agent_runs(self, project_id: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE agent_runs SET status = 'ERROR', error_message = 'interrupted: controller stopped before completion', finished_at = ? "
                "WHERE project_id = ? AND status = 'RUNNING'",
                (utcnow(), project_id),
            )
            return cur.rowcount

    def run_cost(self, run_id: int) -> float:
        row = self.query_one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_runs WHERE run_id = ?", (run_id,))
        return float(row["c"]) if row else 0.0

    def task_turns(self, task_id: int) -> int:
        row = self.query_one("SELECT COALESCE(SUM(num_turns), 0) AS n FROM agent_runs WHERE task_id = ?", (task_id,))
        return int(row["n"]) if row else 0

    def list_agent_runs(self, project_id: str, task_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if task_id is not None:
            return self.query("SELECT * FROM agent_runs WHERE project_id = ? AND task_id = ? ORDER BY id DESC LIMIT ?", (project_id, task_id, limit))
        return self.query("SELECT * FROM agent_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit))

    # ---------------------------------------------------------------- issues
    def add_issue(
        self,
        project_id: str,
        task_id: int | None,
        title: str,
        severity: str,
        category: str,
        source_agent: str,
        description: str = "",
        file_path: str | None = None,
        line_number: int | None = None,
        recommendation: str | None = None,
    ) -> tuple[int, bool]:
        fp = fingerprint(task_id or "", category, file_path or "", title)
        now = utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id, status FROM issues WHERE project_id = ? AND fingerprint = ? AND status IN ('OPEN','FIXED_PENDING_VERIFICATION','REOPENED')",
                (project_id, fp),
            ).fetchone()
            if row:
                if row["status"] == "FIXED_PENDING_VERIFICATION":
                    conn.execute("UPDATE issues SET status = 'REOPENED', updated_at = ? WHERE id = ?", (now, row["id"]))
                return int(row["id"]), False
            issue_id = self._insert(
                conn,
                "issues",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "title": title[:500],
                    "severity": severity.upper(),
                    "category": category,
                    "source_agent": source_agent,
                    "file_path": file_path,
                    "line_number": line_number,
                    "description": description[:8000],
                    "recommendation": (recommendation or "")[:4000],
                    "fingerprint": fp,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return issue_id, True

    def open_issues(self, task_id: int, categories: Iterable[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM issues WHERE task_id = ? AND status IN ('OPEN', 'REOPENED')"
        params: list[Any] = [task_id]
        if categories:
            categories = list(categories)
            sql += f" AND category IN ({','.join('?' for _ in categories)})"
            params.extend(categories)
        return self.query(sql + " ORDER BY id", params)

    def issues_for_task(self, task_id: int) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM issues WHERE task_id = ? ORDER BY id", (task_id,))

    def set_issue_status(self, issue_ids: Iterable[int], status: str, fix_summary: str | None = None, increment_attempts: bool = False) -> None:
        now = utcnow()
        with self.transaction() as conn:
            for issue_id in issue_ids:
                updates: dict[str, Any] = {"status": status, "updated_at": now}
                if fix_summary is not None:
                    updates["fix_summary"] = fix_summary[:4000]
                if status in {"VERIFIED", "WONT_FIX"}:
                    updates["resolved_at"] = now
                self._update(conn, "issues", "id", issue_id, updates)
                if increment_attempts:
                    conn.execute("UPDATE issues SET fix_attempts = fix_attempts + 1 WHERE id = ?", (issue_id,))

    def verify_pending_issues(self, task_id: int, categories: Iterable[str]) -> int:
        """Called when an independent gate (review/QA/security/validation) passes: every
        unresolved issue of those categories for the task is now verified as resolved."""
        categories = list(categories)
        marks = ",".join("?" for _ in categories)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE issues SET status = 'VERIFIED', resolved_at = ?, updated_at = ? "
                f"WHERE task_id = ? AND status IN ('FIXED_PENDING_VERIFICATION','OPEN','REOPENED') AND category IN ({marks})",
                (utcnow(), utcnow(), task_id, *categories),
            )
            return cur.rowcount

    # ----------------------------------------------------------- checkpoints
    def add_checkpoint(self, project_id: str, task_id: int | None, git_commit: str, branch: str, description: str) -> int:
        with self.transaction() as conn:
            return self._insert(
                conn,
                "checkpoints",
                {"project_id": project_id, "task_id": task_id, "git_commit": git_commit, "branch": branch, "description": description, "created_at": utcnow()},
            )

    def list_checkpoints(self, project_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM checkpoints WHERE project_id = ? ORDER BY id DESC", (project_id,))

    # ------------------------------------------------------------- test runs
    def add_test_run(self, project_id: str, task_id: int | None, stage: str | None, **fields: Any) -> int:
        cases = fields.pop("cases", [])
        with self.transaction() as conn:
            run_id = self._insert(conn, "test_runs", {"project_id": project_id, "task_id": task_id, "stage": stage, "created_at": utcnow(), **fields})
            for case in cases:
                self._insert(
                    conn,
                    "test_cases",
                    {
                        "test_run_id": run_id,
                        "nodeid": case.get("nodeid", "")[:1000],
                        "status": case.get("status", "UNKNOWN"),
                        "duration_seconds": case.get("duration_seconds"),
                        "message": (case.get("message") or "")[:4000],
                    },
                )
            return run_id

    def test_cases(self, test_run_id: int) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM test_cases WHERE test_run_id = ? ORDER BY id", (test_run_id,))

    def list_test_runs(self, project_id: str, task_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if task_id is not None:
            return self.query("SELECT * FROM test_runs WHERE project_id = ? AND task_id = ? ORDER BY id DESC LIMIT ?", (project_id, task_id, limit))
        return self.query("SELECT * FROM test_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit))

    # -------------------------------------------------------------- security
    def add_security_scan(self, project_id: str, task_id: int | None, **fields: Any) -> int:
        with self.transaction() as conn:
            return self._insert(conn, "security_scans", {"project_id": project_id, "task_id": task_id, "created_at": utcnow(), **fields})

    def upsert_security_finding(self, project_id: str, task_id: int | None, scan_id: int, finding: dict[str, Any], baseline: bool) -> tuple[int, bool]:
        now = utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id, status FROM security_findings WHERE project_id = ? AND fingerprint = ?",
                (project_id, finding["fingerprint"]),
            ).fetchone()
            if row:
                updates: dict[str, Any] = {"scan_id": scan_id, "updated_at": now, "line_number": finding.get("line_number")}
                if row["status"] == "FIXED":  # it came back
                    updates["status"] = "OPEN"
                    updates["resolved_at"] = None
                self._update(conn, "security_findings", "id", row["id"], updates)
                return int(row["id"]), False
            finding_id = self._insert(
                conn,
                "security_findings",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "scan_id": scan_id,
                    "tool_name": finding["tool_name"],
                    "rule_id": finding.get("rule_id"),
                    "severity": finding["severity"],
                    "confidence": finding.get("confidence"),
                    "file_path": finding.get("file_path"),
                    "line_number": finding.get("line_number"),
                    "description": finding["description"][:4000],
                    "status": "OPEN",
                    "baseline": baseline,
                    "fingerprint": finding["fingerprint"],
                    "report_path": finding.get("report_path"),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return finding_id, True

    def resolve_missing_findings(self, project_id: str, tool_name: str, present_fingerprints: set[str]) -> list[int]:
        rows = self.query(
            "SELECT id, fingerprint FROM security_findings WHERE project_id = ? AND tool_name = ? AND status IN ('OPEN','VERIFIED','FALSE_POSITIVE_SUSPECTED','NEEDS_REVIEW')",
            (project_id, tool_name),
        )
        resolved = [r["id"] for r in rows if r["fingerprint"] not in present_fingerprints]
        if resolved:
            now = utcnow()
            with self.transaction() as conn:
                for finding_id in resolved:
                    conn.execute("UPDATE security_findings SET status = 'FIXED', resolved_at = ?, updated_at = ? WHERE id = ?", (now, now, finding_id))
        return resolved

    def open_security_findings(self, project_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM security_findings WHERE project_id = ? AND status IN ('OPEN','VERIFIED','FALSE_POSITIVE_SUSPECTED','NEEDS_REVIEW') ORDER BY id",
            (project_id,),
        )

    def update_security_finding(self, finding_id: int, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.transaction() as conn:
            self._update(conn, "security_findings", "id", finding_id, fields)

    def list_security_findings(self, project_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM security_findings WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit))

    def list_security_scans(self, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM security_scans WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit))

    # ----------------------------------------------------------- deployments
    def add_deployment(self, project_id: str, task_id: int | None, **fields: Any) -> int:
        with self.transaction() as conn:
            return self._insert(conn, "deployments", {"project_id": project_id, "task_id": task_id, "started_at": utcnow(), **fields})

    def update_deployment(self, deployment_id: int, **fields: Any) -> None:
        with self.transaction() as conn:
            self._update(conn, "deployments", "id", deployment_id, fields)

    def active_deployments(self, project_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM deployments WHERE project_id = ? AND status IN ('STARTING','HEALTHY','RUNNING')",
            (project_id,),
        )

    def list_deployments(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit))

    # --------------------------------------------------------- notifications
    def enqueue_notification(self, project_id: str | None, event_type: str, message: str, dedupe_key: str | None, dedupe_seconds: int, status: str = "PENDING") -> int | None:
        now = datetime.now(timezone.utc)
        with self.transaction() as conn:
            if dedupe_key:
                cutoff = (now - timedelta(seconds=dedupe_seconds)).isoformat(timespec="seconds")
                dup = conn.execute(
                    "SELECT id FROM notifications WHERE dedupe_key = ? AND created_at >= ? AND status != 'FAILED'",
                    (dedupe_key, cutoff),
                ).fetchone()
                if dup:
                    return None
            return self._insert(
                conn,
                "notifications",
                {"project_id": project_id, "event_type": event_type, "message": message, "status": status, "dedupe_key": dedupe_key, "created_at": now.isoformat(timespec="seconds")},
            )

    def pending_notifications(self, limit: int = 20, max_attempts: int = 5) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM notifications WHERE status IN ('PENDING','RETRY') AND attempts < ? ORDER BY id LIMIT ?",
            (max_attempts, limit),
        )

    def mark_notification(self, notification_id: int, status: str, error: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE notifications SET status = ?, attempts = attempts + 1, last_error = ?, sent_at = CASE WHEN ? = 'SENT' THEN ? ELSE sent_at END WHERE id = ?",
                (status, error, status, utcnow(), notification_id),
            )

    def list_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------------- events
    def add_event(self, event: dict[str, Any]) -> int:
        with self.transaction() as conn:
            return self._insert(conn, "events", event)

    def events_after(self, after_id: int = 0, limit: int = 500, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            return self.query("SELECT * FROM events WHERE id > ? AND project_id = ? ORDER BY id LIMIT ?", (after_id, project_id, limit))
        return self.query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))

    def purge_old_events(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            return conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount

    # --------------------------------------------------------------- control
    def set_flag(self, key: str, value: str | None, updated_by: str) -> None:
        self.execute(
            "INSERT INTO control_flags (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (key, value, updated_by, utcnow()),
        )

    def get_flag(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM control_flags WHERE key = ?", (key,))
        return row["value"] if row else None

    def add_control_command(self, project_id: str | None, command: str, args: dict[str, Any], requested_by: str) -> int:
        with self.transaction() as conn:
            return self._insert(
                conn,
                "control_commands",
                {"project_id": project_id, "command": command, "args": args, "requested_by": requested_by, "status": "PENDING", "created_at": utcnow()},
            )

    def pending_control_commands(self, project_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM control_commands WHERE status = 'PENDING' AND (project_id = ? OR project_id IS NULL) ORDER BY id",
            (project_id,),
        )

    def mark_control_command(self, command_id: int, status: str, result: str) -> None:
        self.execute(
            "UPDATE control_commands SET status = ?, result = ?, applied_at = ? WHERE id = ?",
            (status, result[:2000], utcnow(), command_id),
        )

    # ------------------------------------------------------------- approvals
    def request_approval(self, project_id: str, kind: str, subject: str, details: dict[str, Any]) -> tuple[int, str]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id, status FROM approvals WHERE project_id = ? AND kind = ? AND subject = ? ORDER BY id DESC LIMIT 1",
                (project_id, kind, subject),
            ).fetchone()
            if row and row["status"] in ("PENDING", "APPROVED"):
                return int(row["id"]), row["status"]
            approval_id = self._insert(
                conn,
                "approvals",
                {"project_id": project_id, "kind": kind, "subject": subject, "details": details, "status": "PENDING", "requested_at": utcnow()},
            )
            return approval_id, "PENDING"

    def decide_approval(self, approval_id: int, approved: bool, decided_by: str, note: str = "") -> None:
        self.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decision_note = ?, decided_at = ? WHERE id = ? AND status = 'PENDING'",
            ("APPROVED" if approved else "REJECTED", decided_by, note, utcnow(), approval_id),
        )

    def approval_status(self, project_id: str, kind: str, subject: str) -> str | None:
        row = self.query_one(
            "SELECT status FROM approvals WHERE project_id = ? AND kind = ? AND subject = ? ORDER BY id DESC LIMIT 1",
            (project_id, kind, subject),
        )
        return row["status"] if row else None

    def list_approvals(self, project_id: str, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.query("SELECT * FROM approvals WHERE project_id = ? AND status = ? ORDER BY id", (project_id, status))
        return self.query("SELECT * FROM approvals WHERE project_id = ? ORDER BY id", (project_id,))

    # ----------------------------------------------------------------- locks
    def acquire_lock(self, name: str, owner: str, pid: int, ttl_seconds: int) -> tuple[bool, dict[str, Any] | None]:
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now:
                return False, dict(row)
            conn.execute(
                "INSERT INTO locks (name, owner, pid, hostname, acquired_at, heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, pid = excluded.pid, hostname = excluded.hostname, "
                "acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at",
                (name, owner, pid, self.hostname, now, now, now + ttl_seconds),
            )
            return True, dict(row) if row else None

    def force_take_lock(self, name: str, owner: str, pid: int, ttl_seconds: int) -> None:
        now = time.time()
        self.execute(
            "INSERT INTO locks (name, owner, pid, hostname, acquired_at, heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, pid = excluded.pid, hostname = excluded.hostname, "
            "acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at",
            (name, owner, pid, self.hostname, now, now, now + ttl_seconds),
        )

    def heartbeat_lock(self, name: str, owner: str, ttl_seconds: int) -> bool:
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE locks SET heartbeat_at = ?, expires_at = ? WHERE name = ? AND owner = ?",
                (now, now + ttl_seconds, name, owner),
            )
            return cur.rowcount == 1

    def release_lock(self, name: str, owner: str) -> None:
        self.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))

    def get_lock(self, name: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM locks WHERE name = ?", (name,))

    # ------------------------------------------------------ file reservations
    def reserve_files(self, project_id: str, task_id: int, paths: Iterable[str]) -> tuple[bool, list[str]]:
        paths = sorted({p.replace("\\", "/").strip("/") for p in paths if p})
        with self.transaction() as conn:
            conflicts = []
            for path in paths:
                row = conn.execute(
                    "SELECT task_id FROM file_reservations WHERE project_id = ? AND path = ?", (project_id, path)
                ).fetchone()
                if row and row["task_id"] != task_id:
                    conflicts.append(path)
            if conflicts:
                return False, conflicts
            for path in paths:
                conn.execute(
                    "INSERT OR REPLACE INTO file_reservations (project_id, path, task_id, created_at) VALUES (?, ?, ?, ?)",
                    (project_id, path, task_id, utcnow()),
                )
            return True, []

    def release_files(self, project_id: str, task_id: int) -> None:
        self.execute("DELETE FROM file_reservations WHERE project_id = ? AND task_id = ?", (project_id, task_id))

    # ----------------------------------------------------------------- audit
    def audit(self, actor: str, action: str, target: str = "", detail: dict[str, Any] | None = None, allowed: bool = True) -> None:
        self.execute(
            "INSERT INTO audit_log (ts, actor, action, target, allowed, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow(), actor, action, target[:2000], int(allowed), json.dumps(detail or {}, default=str)[:8000]),
        )

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # -------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utcnow()),
        )
