"""Access to the controller's SQLite state database.

Reads use a read-only connection (``mode=ro``). The only writes the dashboard
performs are control flags (pause/resume/stop) and audit-log entries; every other
control action is delegated to the controller CLI.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JSON_COLUMNS = {"acceptance_criteria", "related_modules", "test_cases", "validation_evidence", "file_scope", "design", "metadata", "detail",
                "details", "args", "scan_summary", "output_json", "auditor_assessment", "app_types"}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def _connect(self, readonly: bool = True) -> sqlite3.Connection:
        if readonly:
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in JSON_COLUMNS.intersection(data):
            if isinstance(data[key], str) and data[key]:
                try:
                    data[key] = json.loads(data[key])
                except json.JSONDecodeError:
                    pass
        return data

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        if not self.available():
            return []
        conn = self._connect()
        try:
            return [self._decode(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        finally:
            conn.close()

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def schema_version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"]) if row and row["v"] else 0

    # ------------------------------------------------------------- writes
    def set_flag(self, key: str, value: str, actor: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect(readonly=False)
        try:
            conn.execute(
                "INSERT INTO control_flags (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (key, value, actor, now),
            )
            conn.commit()
        finally:
            conn.close()

    def audit(self, actor: str, action: str, target: str, detail: dict[str, Any], allowed: bool = True) -> None:
        if not self.available():
            return
        conn = self._connect(readonly=False)
        try:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, allowed, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), actor, action, target[:500], int(allowed), json.dumps(detail)[:4000]),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ helpers
    def controller_state(self, project_id: str) -> dict[str, Any]:
        flags = {r["key"]: r["value"] for r in self.query("SELECT key, value FROM control_flags WHERE key IN (?, ?)", (f"pause:{project_id}", f"stop:{project_id}"))}
        lock = self.one("SELECT * FROM locks WHERE name = ?", (f"orchestrator:{project_id}",))
        running = bool(lock and lock["expires_at"] > time.time())
        state = "RUNNING" if running else "IDLE"
        if flags.get(f"pause:{project_id}") == "1":
            state = "PAUSED" if running else "PAUSED (not running)"
        if flags.get(f"stop:{project_id}") == "1" and running:
            state = "STOPPING"
        return {"state": state, "running": running, "paused": flags.get(f"pause:{project_id}") == "1",
                "stop_requested": flags.get(f"stop:{project_id}") == "1", "lock": lock}
