"""SQLite persistence.

Stores only structured, non-pixel data:

* tasks and their step/verification history (for resume)
* reusable workflows
* audit events for assisted actions and privacy events
* model usage counters and non-sensitive preferences
* observation *metadata* (counts, sources, change ratio) — never screenshots

Access is serialised through a lock because the HTTP server is threaded and
SQLite connections are not safe to share across threads without care. WAL mode
keeps reads fast while a write is in flight.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .. import util

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    state TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    app_context TEXT,
    summary TEXT,
    workflow_id TEXT,
    task_json TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    created_at INTEGER NOT NULL,
    session_type TEXT,
    scope TEXT,
    element_count INTEGER,
    change_ratio REAL,
    sensitive INTEGER DEFAULT 0,
    sources_json TEXT
);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    fingerprint TEXT,
    workflow_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    task_id TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    role TEXT,
    provider TEXT,
    model TEXT,
    ok INTEGER,
    tokens INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_events(at DESC);
"""


class Database:
    """Thread-safe SQLite wrapper."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else util.database_path()
        util.ensure_dir(self.path.parent)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._set_schema_version()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _set_schema_version(self) -> None:
        row = self._conn.execute("SELECT value FROM preferences WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO preferences(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- tasks -------------------------------------------------------------
    def save_task(self, task: dict[str, Any]) -> None:
        payload = json.dumps(task, separators=(",", ":"))
        completed_at = task.get("updated_at") if task.get("state") in {"COMPLETED"} else None
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks(id, objective, state, current_index, created_at, updated_at, completed_at,
                                  app_context, summary, workflow_id, task_json, archived)
                VALUES(?,?,?,?,?,?,?,?,?,?,?, COALESCE((SELECT archived FROM tasks WHERE id=?), 0))
                ON CONFLICT(id) DO UPDATE SET
                    objective=excluded.objective,
                    state=excluded.state,
                    current_index=excluded.current_index,
                    updated_at=excluded.updated_at,
                    completed_at=COALESCE(excluded.completed_at, tasks.completed_at),
                    app_context=excluded.app_context,
                    summary=excluded.summary,
                    workflow_id=excluded.workflow_id,
                    task_json=excluded.task_json
                """,
                (
                    task["id"],
                    task.get("objective", ""),
                    task.get("state", "CREATED"),
                    int(task.get("current_index", 0)),
                    int(task.get("created_at", util.now_ms())),
                    int(task.get("updated_at", util.now_ms())),
                    completed_at,
                    task.get("app_context", ""),
                    task.get("summary", ""),
                    task.get("workflow_id"),
                    payload,
                    task["id"],
                ),
            )

    def load_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT task_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["task_json"])

    def list_tasks(self, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
        query = (
            "SELECT id, objective, state, current_index, created_at, updated_at, completed_at, app_context, summary "
            "FROM tasks {} ORDER BY updated_at DESC LIMIT ?"
        ).format("" if include_archived else "WHERE archived=0")
        with self._lock:
            rows = self._conn.execute(query, (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def delete_task(self, task_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            return cursor.rowcount > 0

    def clear_tasks(self) -> int:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM observations")
            return cursor.rowcount

    def find_resumable_task(self) -> dict[str, Any] | None:
        """Most recent unfinished task (for the resume prompt)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM tasks WHERE state NOT IN ('COMPLETED','CANCELLED') ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self.load_task(row["id"])

    # -- observations (metadata only) --------------------------------------
    def add_observation_meta(
        self,
        observation_id: str,
        task_id: str | None,
        session_type: str,
        scope: str,
        element_count: int,
        change_ratio: float,
        sensitive: bool,
        sources: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO observations(id, task_id, created_at, session_type, scope, element_count, "
                "change_ratio, sensitive, sources_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    observation_id,
                    task_id,
                    util.now_ms(),
                    session_type,
                    scope,
                    int(element_count),
                    float(change_ratio),
                    1 if sensitive else 0,
                    json.dumps(sources, separators=(",", ":"))[:8000],
                ),
            )

    # -- workflows ---------------------------------------------------------
    def save_workflow(self, workflow: dict[str, Any]) -> str:
        workflow_id = workflow.get("id") or f"wf-{uuid.uuid4().hex[:10]}"
        workflow["id"] = workflow_id
        now = util.now_ms()
        workflow.setdefault("created_at", now)
        workflow["updated_at"] = now
        payload = json.dumps(workflow, separators=(",", ":"))
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflows(id, name, created_at, updated_at, fingerprint, workflow_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    updated_at=excluded.updated_at,
                    fingerprint=excluded.fingerprint,
                    workflow_json=excluded.workflow_json
                """,
                (
                    workflow_id,
                    workflow.get("name", "Untitled workflow"),
                    int(workflow["created_at"]),
                    now,
                    workflow.get("fingerprint", ""),
                    payload,
                ),
            )
        return workflow_id

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT workflow_json FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        return json.loads(row["workflow_json"]) if row else None

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, updated_at, fingerprint FROM workflows ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
            return cursor.rowcount > 0

    # -- audit -------------------------------------------------------------
    def add_audit(self, kind: str, task_id: str | None, detail: str | dict[str, Any]) -> None:
        payload = detail if isinstance(detail, str) else json.dumps(detail, separators=(",", ":"))
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_events(at, kind, task_id, detail) VALUES(?,?,?,?)",
                (util.now_ms(), kind, task_id, util.truncated(payload, 4000)),
            )

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, kind, task_id, detail FROM audit_events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_audit(self) -> int:
        with self.transaction() as conn:
            return conn.execute("DELETE FROM audit_events").rowcount

    # -- model usage -------------------------------------------------------
    def log_model_call(
        self,
        role: str,
        provider: str,
        model: str,
        ok: bool,
        tokens: int = 0,
        error: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO model_calls(at, role, provider, model, ok, tokens, error) VALUES(?,?,?,?,?,?,?)",
                (util.now_ms(), role, provider, model, 1 if ok else 0, int(tokens), util.truncated(error, 500)),
            )

    def usage_summary(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, provider, model, COUNT(*) AS calls, SUM(ok) AS successes, SUM(tokens) AS tokens "
                "FROM model_calls GROUP BY role, provider, model"
            ).fetchall()
        return {
            "by_role": [dict(row) for row in rows],
            "total_calls": sum(int(r["calls"] or 0) for r in rows),
        }

    # -- preferences -------------------------------------------------------
    def set_preference(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO preferences(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_preference(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_last_task(self, task_id: str | None) -> None:
        self.set_preference("last_task_id", task_id)

    def get_last_task_id(self) -> str | None:
        value = self.get_preference("last_task_id")
        return value if isinstance(value, str) and value else None

    # -- privacy -----------------------------------------------------------
    def purge_all(self) -> dict[str, int]:
        """Delete all local task/workflow/audit data (privacy: clear local data)."""
        with self.transaction() as conn:
            tasks = conn.execute("DELETE FROM tasks").rowcount
            workflows = conn.execute("DELETE FROM workflows").rowcount
            audit = conn.execute("DELETE FROM audit_events").rowcount
            observations = conn.execute("DELETE FROM observations").rowcount
            conn.execute("DELETE FROM preferences WHERE key != 'schema_version'")
        return {
            "tasks": tasks,
            "workflows": workflows,
            "audit_events": audit,
            "observations": observations,
        }
