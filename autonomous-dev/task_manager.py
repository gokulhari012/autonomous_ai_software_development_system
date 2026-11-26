"""Requirements registry and task management.

* Requirements from the project's own documents are APPROVED automatically;
  inferred requirements stay PROPOSED (their tasks ON_HOLD) until a human
  approves them - "implement only approved requirements".
* Duplicate requirements/tasks are prevented by fingerprints and title matching.
* Control commands (retry, cancel, approve, reject) are applied here, both by the
  running orchestrator and - when no orchestrator is running - by the CLI.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_models import ProductManagerOutput
from events import EventBus
from state_manager import StateManager

log = logging.getLogger("task_manager")

KIND_COMMIT_PREFIX = {
    "feature": "feat", "bugfix": "fix", "security": "security", "test": "test", "docs": "docs",
    "refactor": "refactor", "infrastructure": "chore",
}


class TaskManager:
    def __init__(self, state: StateManager, events: EventBus | None, project_id: str, max_retries: int, auto_approve_inferred: bool):
        self.state = state
        self.events = events
        self.project_id = project_id
        self.max_retries = max_retries
        self.auto_approve_inferred = auto_approve_inferred

    def _publish(self, event_type: str, summary: str, **kwargs: Any) -> None:
        if self.events:
            self.events.publish(event_type, summary, **kwargs)

    # ---------------------------------------------------------- registration
    def register_requirements(self, output: ProductManagerOutput) -> dict[str, int]:
        """Store the PM's requirements. Returns a title -> requirement id map (existing ones included)."""
        mapping: dict[str, int] = {}
        for item in output.requirements:
            if item.already_implemented:
                status = "EXISTING"
            elif item.source == "documented" or self.auto_approve_inferred:
                status = "APPROVED"
            else:
                status = "PROPOSED"
            req_id, created = self.state.add_requirement(
                self.project_id, item.title, item.description, item.priority, item.acceptance_criteria,
                item.related_modules, item.test_cases, item.source, status, mandatory=item.mandatory and not item.already_implemented,
            )
            mapping[item.title.strip().lower()] = req_id
            if created:
                req = self.state.get_requirement(req_id)
                self._publish("REQUIREMENT_CREATED", f"{req['code']} {item.title} [{status}]", status=status, metadata={"requirement_id": req_id, "source": item.source, "reference": item.source_reference})
        return mapping

    def register_tasks(self, output: ProductManagerOutput, requirement_ids: dict[str, int]) -> list[int]:
        created_ids: list[int] = []
        title_to_task: dict[str, int] = {t["title"].strip().lower(): t["id"] for t in self.state.list_tasks(self.project_id)}
        for item in output.tasks:
            req_id = requirement_ids.get(item.requirement_title.strip().lower())
            if req_id is None:
                match = self.state.query_one(
                    "SELECT id FROM requirements WHERE project_id = ? AND lower(title) = lower(?)", (self.project_id, item.requirement_title.strip())
                )
                req_id = match["id"] if match else None
            requirement = self.state.get_requirement(req_id) if req_id else None
            if requirement and requirement["status"] in ("EXISTING", "COMPLETED", "REJECTED"):
                continue
            criteria = item.acceptance_criteria or (requirement or {}).get("acceptance_criteria") or []
            task_id, created = self.state.create_task(
                self.project_id, item.title, item.description, req_id, item.priority, item.kind, criteria, item.file_scope_hint, self.max_retries,
            )
            title_to_task[item.title.strip().lower()] = task_id
            if created:
                created_ids.append(task_id)
                if requirement and requirement["status"] == "PROPOSED":
                    self.state.transition_task(task_id, to_status="ON_HOLD", reason=f"requirement {requirement['code']} awaits human approval")
                self._publish("TASK_CREATED", f"#{task_id} {item.title}", status="PENDING", task_id=task_id, metadata={"requirement_id": req_id, "kind": item.kind})
        for item in output.tasks:
            task_id = title_to_task.get(item.title.strip().lower())
            for dependency in item.depends_on:
                dep_id = title_to_task.get(dependency.strip().lower())
                if task_id and dep_id and dep_id != task_id:
                    self.state.add_dependency(task_id, dep_id)
        return created_ids

    def ensure_tasks_for_approved_requirements(self) -> list[int]:
        """Every approved requirement needs at least one task; create a default one if the PM gave none."""
        created = []
        for req in self.state.list_requirements(self.project_id, ["APPROVED", "IN_PROGRESS"]):
            row = self.state.query_one("SELECT COUNT(*) AS n FROM tasks WHERE requirement_id = ?", (req["id"],))
            if row and row["n"] == 0:
                task_id, was_created = self.state.create_task(
                    self.project_id, f"Implement {req['title']}", req["description"], req["id"], req["priority"], "feature",
                    req.get("acceptance_criteria") or [], [], self.max_retries,
                )
                if was_created:
                    created.append(task_id)
                    self._publish("TASK_CREATED", f"#{task_id} Implement {req['title']}", status="PENDING", task_id=task_id)
        return created

    def requirement_done(self, requirement_id: int) -> bool:
        tasks = self.state.query("SELECT status FROM tasks WHERE requirement_id = ?", (requirement_id,))
        return bool(tasks) and all(t["status"] in ("COMPLETED", "CANCELLED") for t in tasks) and any(t["status"] == "COMPLETED" for t in tasks)

    # ---------------------------------------------------------- commands
    def apply_command(self, command: str, args: dict[str, Any], actor: str) -> tuple[bool, str]:
        command = command.upper()
        if command == "RETRY_TASK":
            task = self.state.get_task(int(args["task_id"]))
            if not task or task["project_id"] != self.project_id:
                return False, "task not found"
            if task["status"] not in ("BLOCKED", "FAILED"):
                return False, f"only BLOCKED tasks can be retried (task is {task['status']})"
            self.state.transition_task(task["id"], to_status="PENDING", reason=f"retry requested by {actor}", actor=actor, retry_count=0, stage_error_count=0, blocked_reason=None)
            self.state.audit(actor, "retry_task", str(task["id"]))
            return True, f"task #{task['id']} queued for retry at stage {task['stage']}"
        if command == "CANCEL_TASK":
            task = self.state.get_task(int(args["task_id"]))
            if not task or task["project_id"] != self.project_id:
                return False, "task not found"
            if task["status"] in ("COMPLETED", "CANCELLED"):
                return False, f"task is already {task['status']}"
            if task["status"] == "IN_PROGRESS":
                self.state.update_task_fields(task["id"], cancel_requested=1)
                self.state.audit(actor, "cancel_task_requested", str(task["id"]))
                return True, f"task #{task['id']} will be cancelled at the next safe point"
            self.state.transition_task(task["id"], to_status="CANCELLED", reason=f"cancelled by {actor}", actor=actor)
            self.state.audit(actor, "cancel_task", str(task["id"]))
            return True, f"task #{task['id']} cancelled"
        if command in ("APPROVE_REQUIREMENT", "REJECT_REQUIREMENT"):
            req = self.state.get_requirement_by_code(self.project_id, str(args["code"]).upper())
            if not req:
                return False, "requirement not found"
            if req["status"] != "PROPOSED":
                return False, f"requirement is {req['status']}, not PROPOSED"
            approve = command == "APPROVE_REQUIREMENT"
            self.state.update_requirement(req["id"], status="APPROVED" if approve else "REJECTED")
            for task in self.state.query("SELECT * FROM tasks WHERE requirement_id = ? AND status = 'ON_HOLD'", (req["id"],)):
                self.state.transition_task(task["id"], to_status="PENDING" if approve else "CANCELLED", reason=f"requirement {req['code']} {'approved' if approve else 'rejected'} by {actor}", actor=actor)
            self.state.audit(actor, command.lower(), req["code"])
            return True, f"{req['code']} {'approved' if approve else 'rejected'}"
        if command in ("APPROVE", "REJECT"):
            approval_id = int(args["approval_id"])
            row = self.state.query_one("SELECT * FROM approvals WHERE id = ? AND project_id = ?", (approval_id, self.project_id))
            if not row or row["status"] != "PENDING":
                return False, "approval not found or not pending"
            approved = command == "APPROVE"
            self.state.decide_approval(approval_id, approved, actor, str(args.get("note", "")))
            subject = row["subject"]
            if subject.startswith("task:"):
                task = self.state.get_task(int(subject.split(":", 1)[1]))
                if task and task["status"] == "BLOCKED":
                    if approved:
                        self.state.transition_task(task["id"], to_status="PENDING", reason=f"{row['kind']} approved by {actor}", actor=actor, blocked_reason=None, stage_error_count=0)
                    else:
                        self.state.update_task_fields(task["id"], blocked_reason=f"{row['kind']} rejected by {actor}: {args.get('note', '')}")
            self.state.audit(actor, command.lower(), f"approval {approval_id} ({row['kind']} {subject})")
            return True, f"approval {approval_id} {'approved' if approved else 'rejected'}"
        return False, f"unknown command {command}"
