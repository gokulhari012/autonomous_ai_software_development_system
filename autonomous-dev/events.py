"""Structured event publishing.

Events are persisted in the SQLite ``events`` table, which is the authoritative
record. The dashboard tails that table and pushes new rows over WebSockets, so
a dashboard that disconnects can always replay what it missed.

``actual=True`` marks events that describe real activity observed from the
Claude Agent SDK message stream or from commands the orchestrator executed.
``actual=False`` is reserved for inferred/derived information and is shown
differently by the dashboard.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from logging_setup import redact
from state_manager import StateManager, utcnow

log = logging.getLogger("events")

EVENT_TYPES = {
    "AGENT_STARTED", "AGENT_PROGRESS", "AGENT_TOOL_STARTED", "AGENT_TOOL_COMPLETED", "AGENT_TOOL_DENIED",
    "AGENT_COMPLETED", "AGENT_FAILED", "AGENT_RATE_LIMITED",
    "TASK_CREATED", "TASK_STARTED", "TASK_STAGE_CHANGED", "TASK_COMPLETED", "TASK_BLOCKED", "TASK_CANCELLED",
    "REQUIREMENT_CREATED", "REQUIREMENT_COMPLETED",
    "TEST_STARTED", "TEST_COMPLETED", "SECURITY_SCAN_STARTED", "SECURITY_SCAN_COMPLETED", "SECURITY_FINDING",
    "BUG_CREATED", "BUG_FIXED", "REVIEW_COMPLETED",
    "DEPLOYMENT_STARTED", "DEPLOYMENT_COMPLETED", "DEPLOYMENT_FAILED", "DEPLOYMENT_STOPPED",
    "VALIDATION_COMPLETED", "CHECKPOINT_CREATED", "COMMAND_EXECUTED",
    "PROJECT_SCANNED", "PROJECT_STAGE_CHANGED", "PROJECT_COMPLETED", "APPROVAL_REQUIRED",
    "SYSTEM_STARTED", "SYSTEM_PAUSED", "SYSTEM_RESUMED", "SYSTEM_STOPPED", "SYSTEM_RECOVERY", "SYSTEM_ERROR",
    "REPORT_GENERATED",
}

MAX_SUMMARY = 2000
MAX_METADATA = 16000


class EventBus:
    def __init__(self, state: StateManager, project_id: str | None):
        self.state = state
        self.project_id = project_id
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def publish(
        self,
        event_type: str,
        summary: str = "",
        *,
        status: str | None = None,
        task_id: int | None = None,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "orchestrator",
        actual: bool = True,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type}")
        meta_json = redact(json.dumps(metadata or {}, ensure_ascii=False, default=str))
        if len(meta_json) > MAX_METADATA:
            meta_json = json.dumps({"truncated": True, "preview": meta_json[:MAX_METADATA]})
        event = {
            "event_uid": str(uuid.uuid4()),
            "ts": utcnow(),
            "project_id": self.project_id,
            "task_id": task_id,
            "agent_name": agent,
            "event_type": event_type,
            "status": status,
            "summary": redact(summary)[:MAX_SUMMARY],
            "metadata": meta_json,
            "source": source,
            "actual": 1 if actual else 0,
        }
        try:
            event["id"] = self.state.add_event(event)
        except Exception:
            log.exception("failed to persist event %s", event_type)
        level = logging.WARNING if event_type in {"AGENT_FAILED", "TASK_BLOCKED", "SYSTEM_ERROR", "DEPLOYMENT_FAILED"} else logging.INFO
        if event_type not in {"AGENT_PROGRESS", "AGENT_TOOL_STARTED", "AGENT_TOOL_COMPLETED"}:
            log.log(level, "[%s]%s%s %s", event_type, f" task={task_id}" if task_id else "", f" agent={agent}" if agent else "", event["summary"][:300])
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                log.exception("event listener failed")
        return event
