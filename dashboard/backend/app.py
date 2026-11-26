"""FastAPI backend of the monitoring dashboard.

Run (from the dashboard folder):  python -m backend.app      -> http://127.0.0.1:8000

The SQLite database written by the orchestrator is the single source of truth.
REST endpoints read it; the WebSocket streams new rows of the ``events`` table and
replays history from any ``after_id`` so a reconnecting browser never loses events.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .auth import CSRF_COOKIE, SESSION_COOKIE, AuthManager
from .controller import ControlError, ControllerBridge
from .db import Database
from .git_info import git_summary
from .project_admin import ProjectAdmin, SettingsError
from .settings import DASHBOARD_DIR, Settings, load_settings

AGENTS = ["product_manager", "architect", "developer", "code_reviewer", "qa_engineer", "security_auditor", "bug_fixer", "devops_engineer", "product_validator"]
ARTIFACT_SUFFIXES = {".png", ".jpg", ".zip", ".json", ".xml", ".log", ".md", ".txt", ".html", ".webm"}
SENSITIVE_NAMES = {".env", "credentials.json"}


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class ControlBody(BaseModel):
    project: str | None = Field(None, max_length=100)
    task_id: int | None = None
    approval_id: int | None = None
    code: str | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=20000)
    name: str | None = Field(None, max_length=80)
    path: str | None = Field(None, max_length=500)
    mode: Literal["new", "existing", "auto"] | None = None
    with_telegram_bot: bool = False
    max_tasks: int | None = Field(None, ge=1, le=10000)
    resume: bool = False
    # project creation
    brief: str | None = Field(None, max_length=50000)
    settings: dict[str, Any] | None = None


class SettingsBody(BaseModel):
    project: dict[str, Any] = Field(default_factory=dict)
    workflow: dict[str, Any] = Field(default_factory=dict)


class BriefBody(BaseModel):
    brief: str = Field(max_length=50000)


class WsMessage(BaseModel):
    type: Literal["subscribe", "ping"]
    project: str | None = Field(None, max_length=100)
    after_id: int = Field(0, ge=0)


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower() or "project"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    auth = AuthManager(settings.username, settings.password_hash, settings.secret_key, settings.session_hours)
    bridge = ControllerBridge(settings)
    admin = ProjectAdmin(settings, bridge.run_cli)
    app = FastAPI(title="Autonomous Development Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db

    def user(request: Request) -> dict[str, Any]:
        return auth.require(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:"
        return response

    def project_or_default(project: str | None) -> dict[str, Any]:
        if project:
            row = db.one("SELECT * FROM projects WHERE id = ? AND status != 'DELETED'", (project,))
        else:
            row = db.one("SELECT * FROM projects WHERE status != 'DELETED' ORDER BY updated_at DESC LIMIT 1")
        if not row:
            raise HTTPException(404, "no project found - start the controller once or add a project")
        return row

    # ------------------------------------------------------------- auth
    @app.get("/api/health")
    def health():
        return {"status": "ok", "database": db.available(), "auth_configured": auth.configured}

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        client = request.client.host if request.client else "unknown"
        token, csrf = auth.login(client, body.username, body.password)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=settings.secure_cookies, max_age=settings.session_hours * 3600)
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=settings.secure_cookies, max_age=settings.session_hours * 3600)
        db.audit(f"dashboard:{body.username}", "login", client, {})
        return {"ok": True, "username": body.username, "csrf": csrf}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, claims=Depends(user)):
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(request: Request, claims=Depends(user)):
        return {"username": claims["sub"], "csrf": claims["csrf"]}

    # ---------------------------------------------------------- projects
    @app.get("/api/projects")
    def projects(claims=Depends(user)):
        rows = db.query("SELECT id, name, path, workspace_path, mode, status, current_stage, config_path, updated_at FROM projects WHERE status != 'DELETED' ORDER BY updated_at DESC")
        for row in rows:
            row["controller"] = {k: v for k, v in db.controller_state(row["id"]).items() if k != "lock"}
        return rows

    @app.get("/api/overview")
    def overview(project: str | None = None, claims=Depends(user)):
        proj = project_or_default(project)
        pid = proj["id"]
        tasks = db.query("SELECT status FROM tasks WHERE project_id = ?", (pid,))
        reqs = db.query("SELECT status, mandatory FROM requirements WHERE project_id = ?", (pid,))
        counts = Counter(t["status"] for t in tasks)
        mandatory = [r for r in reqs if r["mandatory"] and r["status"] not in ("REJECTED", "EXISTING")]
        controller = db.controller_state(pid)
        active = db.query("SELECT id, agent_name, task_id, stage, started_at, last_activity_at FROM agent_runs WHERE project_id = ? AND status = 'RUNNING' ORDER BY id DESC", (pid,)) if controller["running"] else []
        last_run = db.one("SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 1", (pid,))
        current_task = db.one("SELECT id, title, stage FROM tasks WHERE project_id = ? AND status = 'IN_PROGRESS' ORDER BY updated_at DESC LIMIT 1", (pid,))
        return {
            "project": {k: proj[k] for k in ("id", "name", "path", "workspace_path", "mode", "status", "current_stage", "dev_branch") if k in proj},
            "controller": {k: v for k, v in controller.items() if k != "lock"},
            "tasks": {"total": len(tasks), **counts},
            "requirements": {"total": len(reqs), "mandatory": len(mandatory), "completed": sum(1 for r in mandatory if r["status"] == "COMPLETED"), **Counter(r["status"] for r in reqs)},
            "progress": {"basis": "completed mandatory requirements with validation evidence", "completed": sum(1 for r in mandatory if r["status"] == "COMPLETED"), "total": len(mandatory)},
            "active_agents": active,
            "current_task": current_task,
            "last_run": last_run,
            "recent_events": db.query("SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT 25", (pid,)),
            "pending_approvals": db.query("SELECT * FROM approvals WHERE project_id = ? AND status = 'PENDING'", (pid,)),
            "limit_wait": kv_json(f"limit_wait:{pid}"),
            "last_push": kv_json(f"last_push:{pid}"),
            "attention": attention(proj, controller, last_run),
        }

    def kv_json(key: str) -> dict[str, Any] | None:
        row = db.one("SELECT value FROM kv WHERE key = ?", (key,))
        if not row or not row["value"]:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    def attention(proj: dict[str, Any], controller: dict[str, Any], last_run: dict[str, Any] | None) -> list[dict[str, str]]:
        """Things that need the user, each with a plain-language fix (shown on Overview and Troubleshooting)."""
        pid = proj["id"]
        items: list[dict[str, str]] = []
        for a in db.query("SELECT * FROM approvals WHERE project_id = ? AND status = 'PENDING' ORDER BY id", (pid,)):
            fix = {"QUESTIONS": "Answer the questions in Control Panel > Questions, then press 'Save answers and resume'.",
                   "REQUIREMENT": "Approve or reject the requirement in Control Panel > Approvals.",
                   "TECH_STACK": "Approve or reject the proposed technology stack in Control Panel > Approvals, then start again.",
                   }.get(a["kind"], "Decide it in Control Panel > Approvals.")
            items.append({"level": "action", "title": f"{a['kind'].replace('_', ' ').title()} waiting for you (#{a['id']})", "fix": fix})
        for t in db.query("SELECT id, title, blocked_reason FROM tasks WHERE project_id = ? AND status = 'BLOCKED'", (pid,)):
            reason = t.get("blocked_reason") or ""
            fix = "Fix the cause, then press Retry in the Task Manager."
            if "No module named" in reason or "pip" in reason.lower() or "not installed" in reason:
                fix = "A Python package the app needs is missing in the controller's environment. Install it (see Troubleshooting) or ask for a simpler stack in the project settings, then Retry."
            elif "approval" in reason:
                fix = "Approve the pending item in Control Panel > Approvals; the task resumes automatically."
            elif "security review" in reason:
                fix = "A HIGH/CRITICAL security finding needs a human decision. Review it in the Security Center."
            items.append({"level": "error", "title": f"Task #{t['id']} blocked: {t['title']}", "detail": reason[:500], "fix": fix})
        wait = kv_json(f"limit_wait:{pid}")
        if wait and wait.get("until"):
            items.append({"level": "info", "title": "Waiting for the Claude usage limit to reset", "detail": wait.get("reason", ""),
                          "fix": "Nothing to do; it resumes automatically. Use 'Resume now' to retry immediately."})
        push = kv_json(f"last_push:{pid}")
        if push and not push.get("ok"):
            items.append({"level": "error", "title": "Last GitHub push failed", "detail": push.get("message", ""),
                          "fix": "Check the repository URL in Settings and press 'Test GitHub connection' in Troubleshooting. Commits are kept locally and pushed on the next checkpoint."})
        if last_run and last_run["status"] in ("ERROR", "BLOCKED", "LIMIT_REACHED") and not controller["running"]:
            items.append({"level": "error", "title": f"Last run ended with {last_run['status']}", "detail": last_run.get("stop_reason") or "",
                          "fix": "Read the reason and the logs in Troubleshooting, fix it, then Start again. State is saved."})
        if proj.get("mode") == "new":
            try:
                brief = admin.read_brief(proj)
                if not brief["brief"].strip() or brief["is_template"]:
                    items.append({"level": "action", "title": "Project description is empty", "fix": "Describe the project in Control Panel > Project brief, then Start."})
            except (SettingsError, OSError, ValueError):
                pass
        return items

    # ---------------------------------------------------- settings/brief
    def admin_call(fn, *args):
        try:
            return fn(*args)
        except SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/settings")
    def get_settings(project_id: str, claims=Depends(user)):
        return admin_call(admin.read_settings, project_or_default(project_id))

    @app.put("/api/projects/{project_id}/settings")
    def put_settings(project_id: str, body: SettingsBody, claims=Depends(user)):
        proj = project_or_default(project_id)
        if db.controller_state(proj["id"])["running"]:
            raise HTTPException(409, "stop the controller before changing settings (they are read when a run starts)")
        result = admin_call(admin.update_settings, proj, body.project, body.workflow)
        db.audit(f"dashboard:{claims['sub']}", "update_settings", proj["id"], {"project": sorted(body.project), "workflow": sorted(body.workflow)})
        return result

    @app.get("/api/projects/{project_id}/brief")
    def get_brief(project_id: str, claims=Depends(user)):
        return admin_call(admin.read_brief, project_or_default(project_id))

    @app.put("/api/projects/{project_id}/brief")
    def put_brief(project_id: str, body: BriefBody, claims=Depends(user)):
        proj = project_or_default(project_id)
        result = admin_call(admin.write_brief, proj, body.brief)
        db.audit(f"dashboard:{claims['sub']}", "update_brief", proj["id"], {"chars": len(body.brief)})
        return result

    @app.get("/api/questions")
    def questions(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        return db.query("SELECT * FROM approvals WHERE project_id = ? AND kind = 'QUESTIONS' ORDER BY id DESC", (pid,))

    @app.get("/api/logs")
    def logs(project: str | None = None, lines: int = Query(300, ge=10, le=3000), contains: str | None = Query(None, max_length=100), claims=Depends(user)):
        pid = project_or_default(project)["id"] if project else None
        return admin.tail_logs(lines, pid, contains)

    # ------------------------------------------------------------ agents
    @app.get("/api/agents")
    def agents(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        running = db.controller_state(pid)["running"]
        result = []
        for name in AGENTS:
            runs = db.query("SELECT * FROM agent_runs WHERE project_id = ? AND agent_name = ? ORDER BY id DESC LIMIT 50", (pid, name))
            latest = runs[0] if runs else None
            active = bool(latest and latest["status"] == "RUNNING" and running)
            last_event = db.one("SELECT ts, event_type, summary FROM events WHERE project_id = ? AND agent_name = ? ORDER BY id DESC LIMIT 1", (pid, name))
            duration = None
            if latest and latest.get("started_at"):
                start = datetime.fromisoformat(latest["started_at"])
                end = datetime.fromisoformat(latest["finished_at"]) if latest.get("finished_at") else datetime.now(timezone.utc)
                duration = round((end - start).total_seconds(), 1)
            result.append({
                "name": name,
                "status": "ACTIVE" if active else (latest["status"] if latest else "IDLE"),
                "active": active,
                "current_task_id": latest["task_id"] if active else None,
                "current_activity": last_event["summary"] if active and last_event else None,
                "last_activity": (latest or {}).get("last_activity_at"),
                "last_run": latest,
                "duration_seconds": duration,
                "runs": len(runs),
                "errors": sum(1 for r in runs if r["status"] == "ERROR"),
                "retries": sum(1 for r in runs if (r.get("attempt") or 1) > 1),
            })
        return result

    @app.get("/api/agents/{name}/runs")
    def agent_runs(name: str, project: str | None = None, limit: int = Query(100, le=500), claims=Depends(user)):
        if name not in AGENTS:
            raise HTTPException(404, "unknown agent")
        pid = project_or_default(project)["id"]
        return db.query("SELECT * FROM agent_runs WHERE project_id = ? AND agent_name = ? ORDER BY id DESC LIMIT ?", (pid, name, limit))

    @app.get("/api/agent-runs/{run_id}")
    def agent_run(run_id: int, claims=Depends(user)):
        run = db.one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404, "agent run not found")
        events = [e for e in db.query("SELECT * FROM events WHERE project_id = ? AND agent_name = ? AND ts >= ? ORDER BY id LIMIT 2000", (run["project_id"], run["agent_name"], run["started_at"]))
                  if (e.get("metadata") or {}).get("agent_run_id") == run_id]
        return {"run": run, "events": events}

    # ------------------------------------------------------------ events
    @app.get("/api/events")
    def events(project: str | None = None, after_id: int = 0, before_id: int | None = None, limit: int = Query(300, le=2000), agent: str | None = None,
               task_id: int | None = None, type: str | None = None, q: str | None = Query(None, max_length=200), claims=Depends(user)):
        pid = project_or_default(project)["id"]
        sql, params = "SELECT * FROM events WHERE project_id = ? AND id > ?", [pid, after_id]
        if before_id:
            sql += " AND id < ?"
            params.append(before_id)
        if agent:
            sql += " AND agent_name = ?"
            params.append(agent)
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if type:
            sql += " AND event_type = ?"
            params.append(type)
        if q:
            sql += " AND summary LIKE ?"
            params.append(f"%{q}%")
        order = "DESC" if before_id or not after_id else "ASC"
        rows = db.query(sql + f" ORDER BY id {order} LIMIT ?", [*params, limit])
        return sorted(rows, key=lambda r: r["id"])

    # ------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    def tasks(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        rows = db.query("SELECT t.*, r.code AS requirement_code FROM tasks t LEFT JOIN requirements r ON r.id = t.requirement_id WHERE t.project_id = ? ORDER BY t.priority, t.id", (pid,))
        deps = defaultdict(list)
        for d in db.query("SELECT d.* FROM task_dependencies d JOIN tasks t ON t.id = d.task_id WHERE t.project_id = ?", (pid,)):
            deps[d["task_id"]].append(d["depends_on_task_id"])
        for row in rows:
            row["depends_on"] = deps.get(row["id"], [])
            row.pop("lease_owner", None)
        return rows

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: int, claims=Depends(user)):
        task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task:
            raise HTTPException(404, "task not found")
        return {
            "task": task,
            "requirement": db.one("SELECT * FROM requirements WHERE id = ?", (task["requirement_id"],)) if task["requirement_id"] else None,
            "depends_on": [r["depends_on_task_id"] for r in db.query("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?", (task_id,))],
            "history": db.query("SELECT * FROM task_history WHERE task_id = ? ORDER BY id", (task_id,)),
            "issues": db.query("SELECT * FROM issues WHERE task_id = ? ORDER BY id", (task_id,)),
            "agent_runs": db.query("SELECT id, agent_name, stage, status, output_summary, error_message, started_at, finished_at, cost_usd, num_turns FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,)),
            "test_runs": db.query("SELECT * FROM test_runs WHERE task_id = ? ORDER BY id", (task_id,)),
            "checkpoints": db.query("SELECT * FROM checkpoints WHERE task_id = ?", (task_id,)),
        }

    @app.get("/api/requirements")
    def requirements(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        return db.query("SELECT * FROM requirements WHERE project_id = ? ORDER BY priority, id", (pid,))

    @app.get("/api/approvals")
    def approvals(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        return db.query("SELECT * FROM approvals WHERE project_id = ? ORDER BY id DESC", (pid,))

    # ----------------------------------------------------------- testing
    @app.get("/api/tests")
    def test_runs(project: str | None = None, limit: int = Query(300, le=2000), claims=Depends(user)):
        pid = project_or_default(project)["id"]
        runs = db.query("SELECT * FROM test_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (pid, limit))
        latest: dict[str, dict] = {}
        for run in runs:
            latest.setdefault(run["test_type"], run)
        return {"latest_by_layer": latest, "runs": runs}

    @app.get("/api/tests/{run_id}")
    def test_run(run_id: int, claims=Depends(user)):
        run = db.one("SELECT * FROM test_runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404, "test run not found")
        artifacts = []
        if run.get("artifacts_dir") and Path(run["artifacts_dir"]).exists():
            base = Path(run["artifacts_dir"])
            artifacts = sorted(str(p) for p in base.rglob("*") if p.is_file() and p.suffix in ARTIFACT_SUFFIXES)[:200]
        coverage = None
        if run.get("artifacts_dir") and (Path(run["artifacts_dir"]) / "coverage.json").exists():
            try:
                coverage = json.loads((Path(run["artifacts_dir"]) / "coverage.json").read_text(encoding="utf-8"))["totals"]
            except (ValueError, KeyError):
                coverage = None
        return {"run": run, "cases": db.query("SELECT * FROM test_cases WHERE test_run_id = ? ORDER BY id", (run_id,)), "artifacts": artifacts, "coverage": coverage}

    # ---------------------------------------------------------- security
    @app.get("/api/security")
    def security(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        findings = db.query("SELECT * FROM security_findings WHERE project_id = ? ORDER BY id DESC LIMIT 2000", (pid,))
        return {
            "findings": findings,
            "scans": db.query("SELECT * FROM security_scans WHERE project_id = ? ORDER BY id DESC LIMIT 300", (pid,)),
            "by_severity": Counter(f["severity"] for f in findings if f["status"] not in ("FIXED", "ACCEPTED_EXCEPTION")),
            "by_tool": Counter(f["tool_name"] for f in findings),
        }

    # --------------------------------------------------------------- git
    @app.get("/api/git")
    def git(project: str | None = None, claims=Depends(user)):
        proj = project_or_default(project)
        summary = git_summary(proj.get("workspace_path"))
        summary["checkpoints"] = db.query("SELECT c.*, t.title AS task_title FROM checkpoints c LEFT JOIN tasks t ON t.id = c.task_id WHERE c.project_id = ? ORDER BY c.id DESC", (proj["id"],))
        summary["base_branch"] = proj.get("base_branch")
        summary["destructive_actions"] = "not available from the dashboard"
        return summary

    # -------------------------------------------------------- deployments
    @app.get("/api/deployments")
    def deployments(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        rows = db.query("SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 200", (pid,))
        return {"deployments": rows, "active": [r for r in rows if r["status"] in ("STARTING", "HEALTHY", "RUNNING")],
                "validations": db.query("SELECT id, task_id, status, output_summary, finished_at FROM agent_runs WHERE project_id = ? AND agent_name = 'product_validator' ORDER BY id DESC LIMIT 50", (pid,))}

    # -------------------------------------------------------------- usage
    @app.get("/api/usage")
    def usage(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        runs = db.query("SELECT agent_name, status, backend, input_tokens, output_tokens, cache_read_tokens, cost_usd, duration_ms, num_turns, attempt, started_at FROM agent_runs WHERE project_id = ?", (pid,))
        per_agent: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        timeline: dict[str, float] = defaultdict(float)
        for r in runs:
            a = per_agent[r["agent_name"]]
            a["runs"] += 1
            a["input_tokens"] += r["input_tokens"] or 0
            a["output_tokens"] += r["output_tokens"] or 0
            a["cost_usd"] += r["cost_usd"] or 0
            a["duration_seconds"] += (r["duration_ms"] or 0) / 1000
            a["turns"] += r["num_turns"] or 0
            a["errors"] += 1 if r["status"] == "ERROR" else 0
            timeline[r["started_at"][:13]] += r["cost_usd"] or 0
        controller_runs = db.query("SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC", (pid,))
        total_seconds = 0.0
        for run in controller_runs:
            start = datetime.fromisoformat(run["started_at"])
            end = datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.now(timezone.utc)
            total_seconds += (end - start).total_seconds()
        retries = db.one("SELECT COALESCE(SUM(retry_count), 0) AS n FROM tasks WHERE project_id = ?", (pid,))
        return {
            "per_agent": {k: dict(v) for k, v in per_agent.items()},
            "totals": {"agent_runs": len(runs), "input_tokens": sum(r["input_tokens"] or 0 for r in runs), "output_tokens": sum(r["output_tokens"] or 0 for r in runs),
                       "cost_usd": round(sum(r["cost_usd"] or 0 for r in runs), 4), "task_repair_attempts": retries["n"] if retries else 0,
                       "execution_seconds": round(total_seconds), "scripted_runs": sum(1 for r in runs if r["backend"] == "scripted")},
            "cost_timeline": [{"hour": k, "cost_usd": round(v, 4)} for k, v in sorted(timeline.items())],
            "runs": controller_runs[:50],
            "note": "Costs are those reported by the Claude Agent SDK. With a Claude subscription login they are estimates, not charges.",
        }

    # ------------------------------------------------------------ reports
    @app.get("/api/reports")
    def reports(project: str | None = None, claims=Depends(user)):
        pid = project_or_default(project)["id"]
        folder = settings.reports_dir / "releases"
        files = sorted(folder.glob(f"{pid}-*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
        task_files = sorted((folder / pid).glob("*.md")) if (folder / pid).exists() else []
        return [{"name": p.name, "path": str(p), "modified": p.stat().st_mtime} for p in [*files, *task_files]]

    @app.get("/api/artifact")
    def artifact(path: str = Query(..., max_length=1000), claims=Depends(user)):
        target = Path(path).resolve()
        reports_root = settings.reports_dir.resolve()
        if reports_root not in target.parents or not target.is_file():
            raise HTTPException(404, "artifact not found")
        if target.suffix.lower() not in ARTIFACT_SUFFIXES or target.name.lower() in SENSITIVE_NAMES:
            raise HTTPException(403, "file type not served")
        return FileResponse(target, headers={"Content-Disposition": f'inline; filename="{target.name}"'})

    # ------------------------------------------------------------ control
    @app.post("/api/control/{action}")
    def control(action: str, body: ControlBody, claims=Depends(user)):
        actor = f"dashboard:{claims['sub']}"
        try:
            if action in ("new_project", "open_project", "create_project"):
                if not body.name or not body.path:
                    raise ControlError("name and path are required")
                mode = body.mode or ("new" if action == "new_project" else "existing")
                result = bridge.add_project(body.name, body.path, mode)
                if not result["ok"]:
                    raise ControlError(result["output"][-500:])
                db.audit(actor, action, body.path, {"name": body.name, "mode": mode})
                created = db.one("SELECT * FROM projects WHERE id = ?", (_slug(body.name),))
                if created and body.settings:
                    try:
                        admin.update_settings(created, body.settings, {})
                    except SettingsError as exc:
                        raise ControlError(f"project created, but settings were rejected: {exc}") from exc
                if created and body.brief and body.brief.strip():
                    try:
                        if mode == "new":
                            admin.write_brief(created, body.brief)
                        else:  # existing projects: the description goes to the Product Manager as project notes
                            admin.update_settings(created, {"project_notes": body.brief}, {})
                    except SettingsError as exc:
                        raise ControlError(f"project created, but the description could not be saved: {exc}") from exc
                result["project_id"] = created["id"] if created else None
                return result
            proj = project_or_default(body.project)
            pid, config_path = proj["id"], proj.get("config_path")
            if action == "start":
                state = db.controller_state(pid)
                if state["running"]:
                    raise ControlError("the controller is already running for this project")
                db.set_flag(f"stop:{pid}", "0", actor)
                db.set_flag(f"pause:{pid}", "0", actor)
                result = bridge.start(config_path, pid, body.with_telegram_bot, body.max_tasks)
            elif action == "delete_project":
                if db.controller_state(pid)["running"]:
                    raise ControlError("stop the controller for this project before deleting it")
                result = bridge.run_cli(None, ["project", "delete", pid])
                if not result["ok"]:
                    raise ControlError(result["output"][-400:])
            elif action == "answer_questions":
                if body.approval_id is None or not (body.note or "").strip():
                    raise ControlError("approval_id and the answers are required")
                result = bridge.run_cli(config_path, ["answer", str(int(body.approval_id)), "--text-stdin"], stdin_text=body.note)
                if result["ok"] and body.resume and not db.controller_state(pid)["running"]:
                    db.set_flag(f"stop:{pid}", "0", actor)
                    result["started"] = bridge.start(config_path, pid, body.with_telegram_bot, body.max_tasks)
            elif action == "resume_now":
                result = bridge.run_cli(config_path, ["resume-now"])
            elif action == "doctor":
                result = bridge.run_cli(config_path, ["doctor"], timeout=240)
            elif action == "telegram_test":
                result = bridge.run_cli(config_path, ["telegram-test"], timeout=90)
            elif action == "git_check":
                result = bridge.run_cli(config_path, ["git-check"], timeout=120)
            elif action == "validate_config":
                result = bridge.run_cli(config_path, ["validate-config"], timeout=60)
            elif action in ("pause", "resume", "stop"):
                if action == "pause":
                    db.set_flag(f"pause:{pid}", "1", actor)
                elif action == "resume":
                    db.set_flag(f"pause:{pid}", "0", actor)
                    db.set_flag(f"stop:{pid}", "0", actor)
                else:
                    db.set_flag(f"stop:{pid}", "1", actor)
                result = {"ok": True, "controller": {k: v for k, v in db.controller_state(pid).items() if k != "lock"}}
            elif action in ("retry_task", "cancel_task"):
                if body.task_id is None:
                    raise ControlError("task_id is required")
                result = bridge.run_cli(config_path, [action.replace("_", "-"), str(int(body.task_id))])
            elif action in ("approve", "reject"):
                if body.approval_id is None:
                    raise ControlError("approval_id is required")
                result = bridge.run_cli(config_path, [action, str(int(body.approval_id)), "--note-stdin"], stdin_text=body.note or "")
            elif action in ("approve_requirement", "reject_requirement"):
                result = bridge.run_cli(config_path, [action.replace("_", "-"), bridge.validate_code(body.code or "")])
            elif action == "generate_report":
                result = bridge.run_cli(config_path, ["report"], timeout=300)
            else:
                raise HTTPException(404, "unknown control action")
        except ControlError as exc:
            db.audit(actor, action, str(body.project), {"error": str(exc)}, allowed=False)
            raise HTTPException(400, str(exc)) from exc
        db.audit(actor, action, pid, {"task_id": body.task_id, "approval_id": body.approval_id, "code": body.code})
        return result

    # ---------------------------------------------------------- websocket
    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket):
        claims = auth.require_ws(websocket)
        if claims is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        subscription = {"project": websocket.query_params.get("project"), "after_id": int(websocket.query_params.get("after_id", "0") or 0)}
        invalid = 0

        async def receiver():
            nonlocal invalid
            while True:
                raw = await websocket.receive_text()
                try:
                    message = WsMessage.model_validate_json(raw)
                except ValidationError:
                    invalid += 1
                    await websocket.send_json({"type": "error", "error": "invalid message"})
                    if invalid >= 3:
                        await websocket.close(code=1008)
                        return
                    continue
                if message.type == "ping":
                    await websocket.send_json({"type": "pong", "ts": time.time()})
                else:
                    subscription.update(project=message.project, after_id=message.after_id)

        async def sender():
            while True:
                pid = subscription["project"]
                if pid:
                    rows = db.query("SELECT * FROM events WHERE project_id = ? AND id > ? ORDER BY id LIMIT 500", (pid, subscription["after_id"]))
                else:
                    rows = db.query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 500", (subscription["after_id"],))
                if rows:
                    subscription["after_id"] = rows[-1]["id"]
                    await websocket.send_json({"type": "events", "events": rows, "last_id": rows[-1]["id"]})
                await asyncio.sleep(0.2 if len(rows) == 500 else 0.75)

        tasks = [asyncio.create_task(receiver()), asyncio.create_task(sender())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()

    # ------------------------------------------------------- static files
    dist = DASHBOARD_DIR / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith(("api/", "ws/")):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/")
        def no_frontend():
            return JSONResponse({"message": "frontend not built: run 'npm install && npm run build' in dashboard/frontend"})

    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
