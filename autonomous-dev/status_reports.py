"""Human-readable status summaries shared by the CLI and the Telegram bot."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from config import REPORTS_DIR
from state_manager import StateManager


def control_state(state: StateManager, project_id: str) -> str:
    if state.get_flag(f"stop:{project_id}") == "1":
        return "STOP REQUESTED"
    if state.get_flag(f"pause:{project_id}") == "1":
        return "PAUSED"
    lock = state.get_lock(f"orchestrator:{project_id}")
    import time

    if lock and lock["expires_at"] > time.time():
        return "RUNNING"
    return "IDLE"


def overall_status(state: StateManager, project_id: str) -> str:
    project = state.get_project(project_id) or {}
    tasks = state.list_tasks(project_id)
    counts = Counter(t["status"] for t in tasks)
    reqs = state.list_requirements(project_id)
    req_counts = Counter(r["status"] for r in reqs)
    active = [t for t in tasks if t["status"] == "IN_PROGRESS"]
    lines = [
        f"Project: {project.get('name', project_id)} ({project.get('mode') or 'unknown'} mode)",
        f"Controller: {control_state(state, project_id)}",
        f"Project stage: {project.get('current_stage') or '-'}",
        f"Tasks: {len(tasks)} total | {counts.get('COMPLETED', 0)} completed | {counts.get('IN_PROGRESS', 0)} in progress | "
        f"{counts.get('PENDING', 0)} pending | {counts.get('BLOCKED', 0)} blocked | {counts.get('ON_HOLD', 0)} awaiting approval",
        f"Requirements: {len(reqs)} total | {req_counts.get('COMPLETED', 0)} completed | {req_counts.get('PROPOSED', 0)} awaiting approval",
    ]
    for task in active:
        lines.append(f"Active: #{task['id']} {task['title']} @ {task['stage']} (retries {task['retry_count']}/{task['max_retries']})")
    return "\n".join(lines)


def tasks_summary(state: StateManager, project_id: str, limit: int = 25) -> str:
    tasks = state.list_tasks(project_id)
    if not tasks:
        return "No tasks yet."
    order = {"IN_PROGRESS": 0, "BLOCKED": 1, "PENDING": 2, "ON_HOLD": 3, "COMPLETED": 4, "CANCELLED": 5}
    tasks.sort(key=lambda t: (order.get(t["status"], 9), t["priority"], t["id"]))
    lines = [f"#{t['id']} [{t['status']}] {t['title']} - {t['stage']}" + (f" ({t['blocked_reason'][:80]})" if t["status"] == "BLOCKED" and t.get("blocked_reason") else "") for t in tasks[:limit]]
    if len(tasks) > limit:
        lines.append(f"... {len(tasks) - limit} more")
    return "\n".join(lines)


def agents_summary(state: StateManager, project_id: str) -> str:
    runs = state.list_agent_runs(project_id, limit=300)
    if not runs:
        return "No agent activity yet."
    latest: dict[str, dict] = {}
    totals: Counter = Counter()
    for run in runs:
        totals[run["agent_name"]] += 1
        latest.setdefault(run["agent_name"], run)
    lines = []
    for name, run in sorted(latest.items()):
        active = " (ACTIVE)" if run["status"] == "RUNNING" else ""
        lines.append(f"{name}: last {run['status']}{active} on task #{run['task_id'] or '-'} at {run['started_at']}, {totals[name]} run(s)")
    return "\n".join(lines)


def latest_report(project_id: str) -> tuple[Path | None, str]:
    folder = REPORTS_DIR / "releases"
    candidates = sorted(folder.glob(f"{project_id}-*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    if not candidates:
        return None, "No report has been generated yet."
    path = candidates[0]
    text = path.read_text(encoding="utf-8")
    summary_lines = [l for l in text.splitlines() if l.strip()][:30]
    return path, "\n".join(summary_lines)
