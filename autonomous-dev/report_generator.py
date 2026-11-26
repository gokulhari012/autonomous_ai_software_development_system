"""Final project report and per-task reports (reports/releases/).

Built exclusively from persisted evidence (database rows written by the
orchestrator), never from agent claims. Incomplete and blocked work is always
listed; the completion verdict is computed against the project completion
criteria, not assumed.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from config import REPORTS_DIR, AppConfig
from state_manager import StateManager


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_None._\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ")[:160] for c in row) + " |")
    return "\n".join(out) + "\n"


def completion_criteria(state: StateManager, project_id: str) -> dict[str, Any]:
    reqs = state.list_requirements(project_id)
    mandatory = [r for r in reqs if r["mandatory"] and r["status"] not in ("REJECTED",)]
    tasks = state.list_tasks(project_id)
    open_critical = [f for f in state.open_security_findings(project_id) if f["severity"] == "CRITICAL"]
    checkpoints = state.list_checkpoints(project_id)
    latest_tests: dict[str, dict] = {}
    for run in reversed(state.list_test_runs(project_id, limit=500)):
        latest_tests[run["test_type"]] = run
    deployments = state.list_deployments(project_id, limit=20)
    staged = any(d["status"] in ("HEALTHY", "STOPPED") and d["environment"] == "staging" or d["method"] in ("desktop", "packaged") and d["status"] == "STOPPED" for d in deployments)
    criteria = {
        "all_mandatory_requirements_completed": bool(mandatory) and all(r["status"] == "COMPLETED" for r in mandatory),
        "acceptance_criteria_validated": bool(mandatory) and all(r.get("validation_evidence") for r in mandatory if r["status"] == "COMPLETED"),
        "no_blocked_tasks": not any(t["status"] == "BLOCKED" for t in tasks),
        "latest_required_tests_passed": bool(latest_tests) and all(r["status"] in ("PASS", "NOT_APPLICABLE") or r["test_type"] == "gui" for r in latest_tests.values()),
        "security_scans_completed": any(s["status"] == "COMPLETED" for s in state.list_security_scans(project_id, 50)),
        "no_unresolved_critical_security_issues": not open_critical,
        "local_staging_validated": staged,
        "git_checkpoints_exist": bool(checkpoints),
    }
    criteria["project_complete"] = all(criteria.values())
    return criteria


def generate_final_report(config: AppConfig, state: StateManager, git_log: list[dict[str, str]] | None = None) -> tuple[Path, Path, dict[str, Any]]:
    pid = config.project_id
    project = state.get_project(pid) or {}
    reqs = state.list_requirements(pid)
    tasks = state.list_tasks(pid)
    issues = state.query("SELECT * FROM issues WHERE project_id = ? ORDER BY id", (pid,))
    findings = state.list_security_findings(pid)
    test_runs = state.list_test_runs(pid, limit=1000)
    deployments = state.list_deployments(pid, limit=50)
    checkpoints = state.list_checkpoints(pid)
    agent_runs = state.list_agent_runs(pid, limit=2000)
    criteria = completion_criteria(state, pid)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scan = project.get("scan_summary") or {}

    modified: set[str] = set()
    for run in agent_runs:
        output = run.get("output_json") or {}
        if isinstance(output, dict):
            modified.update(output.get("modified_files") or [])

    def latest_runs(test_type: str) -> list[list[Any]]:
        rows = [r for r in test_runs if r["test_type"] == test_type][:10]
        return [[r["id"], r["task_id"], r["stage"], r["status"], r["total"], r["passed"], r["failed"] + r["errors"], r["created_at"]] for r in rows]

    by_tool = {tool: [f for f in findings if f["tool_name"] == tool] for tool in ("bandit", "semgrep", "pip_audit", "security_auditor")}
    blocked_tasks = [t for t in tasks if t["status"] == "BLOCKED"]
    incomplete_reqs = [r for r in reqs if r["status"] != "COMPLETED"]
    cost = sum((r.get("cost_usd") or 0) for r in agent_runs)
    tokens_in = sum((r.get("input_tokens") or 0) for r in agent_runs)
    tokens_out = sum((r.get("output_tokens") or 0) for r in agent_runs)
    scripted = any(r.get("backend") == "scripted" for r in agent_runs)

    md: list[str] = [f"# Project report: {project.get('name', pid)}", ""]
    md.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}  ")
    md.append(f"Mode: {project.get('mode')} | Branch: {project.get('dev_branch')} | Workspace: `{project.get('workspace_path')}`  ")
    verdict = "COMPLETE" if criteria["project_complete"] else "NOT COMPLETE"
    md.append(f"**Overall verdict: {verdict}**\n")
    if scripted:
        md.append("> **Note:** some agent runs used the scripted test backend (no real Claude calls). Such runs exercise the controller only.\n")
    md += ["## Completion criteria", _table(["Criterion", "Met"], [[k.replace("_", " "), "yes" if v else "NO"] for k, v in criteria.items()])]
    md += ["## Project overview", f"Application types: {', '.join(scan.get('application_types', [])) or '-'}  ",
           f"Flask app: {scan.get('flask_app') or '-'} | PySide: {scan.get('pyside_version') or '-'} | Databases: {', '.join(scan.get('databases', [])) or '-'}\n"]
    md += ["## Requirements", _table(["Code", "Title", "Priority", "Status", "Source", "Evidence"], [[r["code"], r["title"], r["priority"], r["status"], r["source"], "yes" if r.get("validation_evidence") else "-"] for r in reqs])]
    md += ["## Features implemented (tasks)", _table(["#", "Title", "Kind", "Status", "Stage", "Retries", "Completed"], [[t["id"], t["title"], t["kind"], t["status"], t["stage"], t["retry_count"], t.get("completed_at") or "-"] for t in tasks])]
    md += ["## Files modified", "\n".join(f"- `{f}`" for f in sorted(modified)) or "_None recorded._", ""]
    md += ["## Git commits / checkpoints", _table(["Task", "Commit", "Branch", "Description", "Created"], [[c["task_id"], c["git_commit"][:12], c["branch"], c["description"], c["created_at"]] for c in checkpoints])]
    if git_log:
        md += ["Recent commits on the development branch:", "\n".join(f"- `{c['sha'][:10]}` {c['subject']}" for c in git_log[:20]), ""]
    designs = [t for t in tasks if t.get("design")]
    md += ["## Architecture changes", "\n".join(f"- #{t['id']} {t['title']}: {(t['design'] or {}).get('approach', '')[:300]}" for t in designs) or "_None._", ""]
    md += ["## Test results"]
    for label, layer in (("Unit / API / database / integration (pytest)", "pytest"), ("Playwright browser tests", "e2e"), ("PySide pytest-qt tests", "qt"), ("PyAutoGUI desktop tests", "gui"), ("Acceptance tests", "acceptance")):
        md += [f"### {label}", _table(["Run", "Task", "Stage", "Status", "Total", "Passed", "Failed", "When"], latest_runs(layer))]
    md += ["## Security findings"]
    for tool, label in (("bandit", "Bandit"), ("semgrep", "Semgrep"), ("pip_audit", "pip-audit"), ("security_auditor", "Security Auditor (manual)")):
        rows = [[f["id"], f["severity"], f["status"], f"{f.get('file_path') or '-'}:{f.get('line_number') or ''}", f["rule_id"], f["description"][:120]] for f in by_tool[tool]]
        md += [f"### {label}", _table(["#", "Severity", "Status", "Location", "Rule", "Description"], rows)]
    fixed = [i for i in issues if i["status"] == "VERIFIED"]
    remaining = [i for i in issues if i["status"] not in ("VERIFIED", "WONT_FIX")]
    md += ["## Bugs identified and fixed", _table(["#", "Task", "Source", "Severity", "Title", "Fix"], [[i["id"], i["task_id"], i["source_agent"], i["severity"], i["title"], (i.get("fix_summary") or "")[:120]] for i in fixed])]
    md += ["## Remaining issues", _table(["#", "Task", "Source", "Severity", "Status", "Title"], [[i["id"], i["task_id"], i["source_agent"], i["severity"], i["status"], i["title"]] for i in remaining])]
    md += ["## Blocked requirements and tasks", _table(["#", "Task", "Reason"], [[t["id"], t["title"], t.get("blocked_reason") or "-"] for t in blocked_tasks])]
    md += ["### Incomplete requirements", _table(["Code", "Title", "Status"], [[r["code"], r["title"], r["status"]] for r in incomplete_reqs])]
    md += ["## Staging deployment results", _table(["#", "Task", "Env", "Method", "Status", "URL", "Started"], [[d["id"], d["task_id"], d["environment"], d["method"], d["status"], d.get("url") or "-", d["started_at"]] for d in deployments])]
    validations = [r for r in agent_runs if r["agent_name"] == "product_validator"]
    md += ["## Final validation results", _table(["Run", "Task", "Status", "Summary"], [[r["id"], r["task_id"], r["status"], (r.get("output_summary") or r.get("error_message") or "")[:150]] for r in validations[:20]])]
    status_counts = Counter(r["status"] for r in agent_runs)
    md += ["## Usage", f"Agent runs: {len(agent_runs)} ({dict(status_counts)})  ", f"Tokens: {tokens_in:,} input / {tokens_out:,} output  ",
           f"Estimated cost reported by the SDK: ${cost:.2f} (subscription logins are not billed per token; the figure is an estimate)\n"]

    out_dir = REPORTS_DIR / "releases"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{pid}-final-report-{stamp}.md"
    json_path = out_dir / f"{pid}-final-report-{stamp}.json"
    md_path.write_text("\n".join(md), encoding="utf-8")
    data = {
        "project": project.get("name", pid), "generated_at": stamp, "criteria": criteria, "requirements": reqs, "tasks": tasks,
        "issues": issues, "security_findings": findings, "test_runs": test_runs[:200], "deployments": deployments,
        "checkpoints": checkpoints, "usage": {"agent_runs": len(agent_runs), "input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": cost},
    }
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return md_path, json_path, criteria


def write_task_report(config: AppConfig, state: StateManager, task_id: int) -> Path:
    task = state.get_task(task_id) or {}
    out = REPORTS_DIR / "releases" / config.project_id / f"task-{task_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    history = state.task_history(task_id)
    tests = state.list_test_runs(config.project_id, task_id)
    issues = state.issues_for_task(task_id)
    lines = [f"# Task #{task_id}: {task.get('title')}", "", f"Status: **{task.get('status')}** (stage {task.get('stage')}, retries {task.get('retry_count')})", ""]
    lines += ["## Acceptance criteria", "\n".join(f"- {c}" for c in task.get("acceptance_criteria") or []) or "-", ""]
    lines += ["## History", _table(["When", "From", "To", "Status", "Reason"], [[h["created_at"], h["from_stage"], h["to_stage"], h["to_status"], (h["reason"] or "")[:120]] for h in history])]
    lines += ["## Test runs", _table(["Run", "Stage", "Layer", "Status", "Passed", "Failed"], [[t["id"], t["stage"], t["test_type"], t["status"], t["passed"], t["failed"] + t["errors"]] for t in tests])]
    lines += ["## Issues", _table(["#", "Source", "Severity", "Status", "Title"], [[i["id"], i["source_agent"], i["severity"], i["status"], i["title"]] for i in issues])]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
