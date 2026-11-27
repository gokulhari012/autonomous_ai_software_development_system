"""The orchestrator: central controller and workflow state machine.

Project level (per run)::

    PROJECT_SCAN -> [new mode] PROJECT_DISCOVERY -> PROJECT_SPECIFICATION -> PROJECT_ARCHITECTURE
                 -> [existing]  REQUIREMENT_ANALYSIS
                 -> TASK_PLANNING -> task loop -> final report

Task level::

    ARCHITECTURE_DESIGN -> DEVELOPMENT -> CODE_REVIEW -> QA_TESTING -> SECURITY_TESTING
        -> LOCAL_STAGING_DEPLOYMENT -> PRODUCT_VALIDATION -> GIT_CHECKPOINT -> COMPLETED
    failures route to BUG_FIXING (or DEVELOPMENT), which always goes back through
    review/QA/security; after ``max_retries_per_task`` repair attempts -> BLOCKED.

The orchestrator owns task selection, agent execution, status, retries,
transitions, persistent state, git checkpoints, error handling, test execution,
deployment coordination and notifications. Agents never decide workflow
transitions; their structured output is one input to decisions the orchestrator
verifies independently (real test runs, real scans, real health checks).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import psutil

from agent_models import Severity, Status
from agent_runner import (
    AgentRequest,
    AgentResult,
    AgentRunner,
    BudgetExceeded,
    ClaudeAgentBackend,
    FatalAgentError,
    ScriptedBackend,
    TurnLimitExceeded,
    UsageLimitReached,
    is_usage_limit_text,
)
from config import BACKUPS_DIR, REPORTS_DIR, WORKSPACES_DIR, AppConfig, ensure_runtime_dirs, severity_rank
from deployment_manager import DeploymentManager, DeploymentResult
from events import EventBus
from git_manager import GitError, GitManager, GitSafetyError
from logging_setup import redact, register_secret_values
from project_inspector import ProjectProfile, inspect_project
from report_generator import completion_criteria, generate_final_report, write_task_report
from safe_exec import CommandPolicy, SafeExecutor
from security_scanner import SecurityScanner
from state_manager import StateManager, fingerprint
from task_manager import KIND_COMMIT_PREFIX, TaskManager
from telegram_bot import TelegramCommandBot
from telegram_notifier import TelegramNotifier
from test_manager import TestManager, TestSuiteResult

log = logging.getLogger("orchestrator")


class Stage(str, Enum):
    PROJECT_SCAN = "PROJECT_SCAN"
    PROJECT_DISCOVERY = "PROJECT_DISCOVERY"
    PROJECT_SPECIFICATION = "PROJECT_SPECIFICATION"
    PROJECT_ARCHITECTURE = "PROJECT_ARCHITECTURE"
    REQUIREMENT_ANALYSIS = "REQUIREMENT_ANALYSIS"
    TASK_PLANNING = "TASK_PLANNING"
    ARCHITECTURE_DESIGN = "ARCHITECTURE_DESIGN"
    DEVELOPMENT = "DEVELOPMENT"
    CODE_REVIEW = "CODE_REVIEW"
    QA_TESTING = "QA_TESTING"
    SECURITY_TESTING = "SECURITY_TESTING"
    BUG_FIXING = "BUG_FIXING"
    LOCAL_STAGING_DEPLOYMENT = "LOCAL_STAGING_DEPLOYMENT"
    PRODUCT_VALIDATION = "PRODUCT_VALIDATION"
    GIT_CHECKPOINT = "GIT_CHECKPOINT"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


S = Stage
TASK_TRANSITIONS: dict[Stage, set[Stage]] = {
    S.ARCHITECTURE_DESIGN: {S.DEVELOPMENT},
    S.DEVELOPMENT: {S.CODE_REVIEW, S.BUG_FIXING, S.DEVELOPMENT},
    S.CODE_REVIEW: {S.QA_TESTING, S.BUG_FIXING, S.DEVELOPMENT},
    S.QA_TESTING: {S.SECURITY_TESTING, S.BUG_FIXING},
    S.SECURITY_TESTING: {S.LOCAL_STAGING_DEPLOYMENT, S.BUG_FIXING},
    S.BUG_FIXING: {S.CODE_REVIEW, S.QA_TESTING},
    S.LOCAL_STAGING_DEPLOYMENT: {S.PRODUCT_VALIDATION, S.BUG_FIXING, S.LOCAL_STAGING_DEPLOYMENT},
    S.PRODUCT_VALIDATION: {S.GIT_CHECKPOINT, S.BUG_FIXING, S.DEVELOPMENT},
    S.GIT_CHECKPOINT: {S.COMPLETED},
}

PROFILE_KEYS = ("application_types", "flask_app", "has_browser_ui", "pyside_version", "pyside_entry", "databases", "source_dirs", "test_dirs", "test_files", "docs", "requirement_docs")

BRIEF_TEMPLATE = """# Project brief

Describe the application you want built. The Product Manager agent reads this file and
asks follow-up questions (in requirements/open_questions.md) if critical information is missing.

## Purpose
<What problem does the application solve?>

## Target users
<Who uses it?>

## Required features and user workflows
- <feature / workflow>

## Technology preferences
<e.g. Flask web app with SQLite, or PySide6 desktop app; leave empty to get a recommendation>

## Data to store
<entities and important fields>

## UI requirements
<web pages / desktop windows>

## External integrations
<none / list>

## Deployment
<local staging only / other notes>
"""


class StopRun(Exception):
    def __init__(self, reason: str, status: str = "STOPPED"):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ProjectInputRequired(StopRun):
    def __init__(self, reason: str):
        super().__init__(reason, "AWAITING_INPUT")


@dataclass
class StageOutcome:
    next_stage: Stage | None = None
    reason: str = ""
    error: bool = False
    block_reason: str | None = None
    repair: bool = False


class Orchestrator:
    def __init__(self, config: AppConfig, backend: ClaudeAgentBackend | ScriptedBackend | None = None, owner: str | None = None):
        ensure_runtime_dirs()
        register_secret_values(config.secrets.secret_values())
        self.config = config
        self.pid = config.project_id
        self.state = StateManager(config.db_path)
        self.events = EventBus(self.state, self.pid)
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lock_name = f"orchestrator:{self.pid}"
        self.notifier = TelegramNotifier(config.secrets, self.state, self.pid, config.project.project_name, config.workflow.notification_dedupe_seconds)
        self.backend = backend or ClaudeAgentBackend(config)
        self.runner = AgentRunner(config, self.state, self.events, self.backend)
        self.tasks = TaskManager(self.state, self.events, self.pid, config.project.max_retries_per_task, config.project.auto_approve_inferred_requirements)
        self.python = config.project.python_executable or sys.executable
        project_dir = config.project_dir
        self.policy = CommandPolicy(
            allowed_roots=[project_dir, WORKSPACES_DIR, REPORTS_DIR, BACKUPS_DIR, config.worktree_path()],
            python_executables=sorted({self.python, sys.executable}),
            allow_push=config.project.git_push_enabled,
            push_remote=config.project.git_remote,
            push_branch=config.project.development_branch,
            protected_branches={config.project.default_branch, "main", "master"},
        )
        self._push_ready = False
        self.limit_margin_seconds = 60  # wait this long past the reported reset time
        self.limit_probe_seconds = 15 * 60  # retry interval when the reset time is unknown
        self.executor = SafeExecutor(self.policy, self.state)
        self.git = GitManager(self.executor, config.project.commit_author_name, config.project.commit_author_email)
        self.workspace: Path | None = None
        self.profile: ProjectProfile | None = None
        self.test_manager: TestManager | None = None
        self.scanner: SecurityScanner | None = None
        self.deployer: DeploymentManager | None = None
        self.run_id: int | None = None
        self.mode: str | None = None
        self.started = time.monotonic()
        self.stop_reason: str | None = None
        self.stop_status: str | None = None
        self._stop = asyncio.Event()
        self._active: set[int] = set()
        self._serial = asyncio.Lock()
        self._paused_announced = False
        self._reanalysed = False
        self.tasks_completed = 0
        self.tasks_blocked = 0

    # ================================================================ run
    async def run(self, max_tasks: int | None = None, with_telegram_bot: bool = False) -> dict[str, Any]:
        self._stop = asyncio.Event()
        self._acquire_controller_lock()
        self.run_id = self.state.start_run(self.pid, os.getpid())
        self.runner.run_id = self.run_id
        self.state.set_flag(f"stop:{self.pid}", "0", "orchestrator")
        self.events.publish("SYSTEM_STARTED", f"controller {self.owner} started (backend={self.backend.name})", status="RUNNING", metadata={"run_id": self.run_id})
        background = [asyncio.create_task(self._heartbeat_loop()), asyncio.create_task(self.notifier.run_worker(self._stop))]
        if with_telegram_bot:
            background.append(asyncio.create_task(TelegramCommandBot(self.config, self.state).run(self._stop)))
        status, reason = "COMPLETED", "no more runnable tasks"
        try:
            self._recover()
            self._prepare_workspace()
            self._apply_control_commands()
            self.state.update_project(self.pid, status="ACTIVE")
            while True:
                try:
                    await self._project_stages()
                    reason = await self._task_loop(max_tasks or self.config.project.max_tasks_per_run)
                    break
                except UsageLimitReached as exc:  # raised by project-level agents (planning, analysis)
                    await self._handle_usage_limit(exc)
                    if self._stop_requested():
                        reason = self.stop_reason or "stopped while waiting for the usage limit"
                        break
            status = self.stop_status or ("STOPPED" if self.stop_reason else "COMPLETED")
            await self._finish_project_if_done()
        except ProjectInputRequired as exc:
            status, reason = exc.status, exc.reason
            self.state.update_project(self.pid, status="AWAITING_INPUT")
            self.events.publish("APPROVAL_REQUIRED", exc.reason, status="BLOCKED")
            self.notifier.notify("APPROVAL_REQUIRED", "Human input required", {"Status": "AWAITING INPUT"}, [exc.reason])
        except StopRun as exc:
            status, reason = exc.status, exc.reason
        except GitSafetyError as exc:
            status, reason = "BLOCKED", f"git safety check failed: {exc}"
            self.events.publish("SYSTEM_ERROR", reason, status="BLOCKED")
            self.notifier.notify("SYSTEM_ERROR", "Autonomous development blocked", {"Reason": "Git safety"}, [str(exc)])
        except Exception as exc:
            status, reason = "ERROR", f"{type(exc).__name__}: {exc}"
            log.exception("orchestrator failure")
            self.events.publish("SYSTEM_ERROR", redact(reason), status="ERROR")
            self.notifier.notify("SYSTEM_ERROR", "Controller error", {"Error": redact(reason)[:300]}, ["The run stopped; state is saved and can be resumed."])
        finally:
            if self.deployer:
                try:
                    self.deployer.stop_all()
                except Exception:
                    log.exception("failed to stop staging deployments")
            project_row = self.state.get_project(self.pid)
            if project_row and project_row.get("current_stage") != "COMPLETED":
                self.state.update_project(self.pid, current_stage="IDLE")
            self.state.finish_run(self.run_id, status, reason)
            self.events.publish("SYSTEM_STOPPED", f"controller stopped: {reason}", status=status, metadata={"run_id": self.run_id, "tasks_completed": self.tasks_completed, "tasks_blocked": self.tasks_blocked})
            if status == "STOPPED":
                self.notifier.notify("SYSTEM_STOPPED", "Autonomous development stopped", {"Reason": reason, "Tasks completed this run": self.tasks_completed})
            self._stop.set()
            await asyncio.gather(*background, return_exceptions=True)
            self.state.release_lock(self.lock_name, self.owner)
        return {"status": status, "reason": reason, "run_id": self.run_id, "tasks_completed": self.tasks_completed, "tasks_blocked": self.tasks_blocked}

    # ======================================================== locking/recovery
    def _acquire_controller_lock(self) -> None:
        ttl = max(self.config.workflow.heartbeat_seconds * 4, 120)
        ok, holder = self.state.acquire_lock(self.lock_name, self.owner, os.getpid(), ttl)
        if ok:
            return
        assert holder is not None
        same_host = holder.get("hostname") == socket.gethostname()
        alive = same_host and holder.get("pid") and psutil.pid_exists(int(holder["pid"]))
        if alive:
            raise StopRun(f"another orchestrator (pid {holder['pid']}) is already running for project {self.pid}", "REFUSED")
        if not same_host:
            raise StopRun(f"project {self.pid} is locked by a controller on host {holder.get('hostname')}; wait for the lease to expire", "REFUSED")
        log.warning("taking over lock from dead controller pid %s", holder.get("pid"))
        self.state.force_take_lock(self.lock_name, self.owner, os.getpid(), ttl)

    async def _heartbeat_loop(self) -> None:
        ttl = max(self.config.workflow.heartbeat_seconds * 4, 120)
        while not self._stop.is_set():
            try:
                self.state.heartbeat_lock(self.lock_name, self.owner, ttl)
                for task_id in list(self._active):
                    self.state.renew_task_lease(task_id, self.owner, self.config.workflow.lease_seconds)
            except Exception:
                log.exception("heartbeat failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.workflow.heartbeat_seconds)
            except asyncio.TimeoutError:
                pass

    def _recover(self) -> None:
        """Inspect what a previous (crashed or interrupted) run left behind before doing anything."""
        notes: list[str] = []
        stale_runs = self.state.mark_stale_runs(self.pid, "controller restarted")
        if stale_runs:
            notes.append(f"{stale_runs} previous run(s) did not shut down cleanly")
        interrupted = self.state.interrupt_open_stage_runs(self.pid)
        for row in interrupted:
            notes.append(f"stage {row['stage']} of task #{row['task_id'] or '-'} was interrupted and will be re-run after inspection")
        agents = self.state.interrupt_running_agent_runs(self.pid)
        if agents:
            notes.append(f"{agents} agent run(s) were interrupted")
        leases = self.state.release_stale_leases(self.pid)
        if leases:
            notes.append(f"released {leases} expired task lease(s)")
        for row in self.state.active_deployments(self.pid):
            pid = row.get("pid")
            alive = False
            if pid:
                try:
                    proc = psutil.Process(pid)
                    alive = row.get("pid_create_time") is None or abs(proc.create_time() - float(row["pid_create_time"])) < 1.0
                except psutil.NoSuchProcess:
                    alive = False
            if alive:
                from safe_exec import kill_process_tree

                kill_process_tree(pid)
                notes.append(f"stopped stale staging process pid {pid} (deployment #{row['id']})")
            self.state.update_deployment(row["id"], status="STOPPED", health_status="stopped during recovery")
        requeued = self._requeue_limit_blocked_tasks()
        if requeued:
            notes.append(f"re-queued task(s) {', '.join('#' + str(t) for t in requeued)} that were blocked only by the Claude usage limit")
        if notes:
            self.events.publish("SYSTEM_RECOVERY", "; ".join(notes)[:1500], status="RECOVERED", metadata={"notes": notes})
            self.notifier.notify("SYSTEM_ERROR", "Recovered after interruption", {"Status": "RESUMING"}, notes)

    def _requeue_limit_blocked_tasks(self) -> list[int]:
        """Tasks blocked because the plan limit made their agents fail are not broken: queue them again.

        Older controller versions did not recognise the CLI's "You've hit your 5-hour limit" error and blocked
        the task after the stage-error limit; the code and the task itself were fine."""
        requeued = []
        for task in self.state.list_tasks(self.pid):
            if task["status"] == "BLOCKED" and is_usage_limit_text(task.get("blocked_reason")):
                self.state.transition_task(task["id"], to_status="PENDING", reason="blocked only by the Claude usage limit; re-queued at controller start",
                                           actor="orchestrator", stage_error_count=0, blocked_reason=None)
                self.state.audit("orchestrator", "requeue_limit_blocked_task", str(task["id"]))
                requeued.append(task["id"])
        return requeued

    def _no_work_reason(self) -> str:
        blocked = [t for t in self.state.list_tasks(self.pid) if t["status"] == "BLOCKED"]
        on_hold = [t for t in self.state.list_tasks(self.pid) if t["status"] == "ON_HOLD"]
        if not blocked and not on_hold:
            return "no more runnable tasks"
        parts = []
        if blocked:
            parts.append(f"{len(blocked)} blocked task(s) ({', '.join('#' + str(t['id']) for t in blocked[:10])}) need Retry in the Task Manager")
        if on_hold:
            parts.append(f"{len(on_hold)} task(s) wait for requirement approval")
        return "no runnable tasks: " + "; ".join(parts)

    # ======================================================= workspace/profile
    def _prepare_workspace(self) -> None:
        cfg = self.config.project
        before = inspect_project(self.config.project_dir, cfg.project_brief)
        mode = cfg.project_mode
        if mode == "auto":
            if before.detected_mode == "ambiguous":
                raise ProjectInputRequired(
                    f"Cannot tell whether {self.config.project_dir} is a new or an existing project ({'; '.join(before.notes)}). "
                    "Set project_mode to 'new' or 'existing' in the project configuration."
                )
            mode = before.detected_mode
        elif mode == "existing" and before.detected_mode == "new":
            raise ProjectInputRequired(f"project_mode is 'existing' but {self.config.project_dir} contains no application code.")
        elif mode == "new" and before.detected_mode == "existing" and not (self.state.get_project(self.pid) or {}).get("mode"):
            raise ProjectInputRequired(f"project_mode is 'new' but {self.config.project_dir} already contains Python code. Use 'existing' or an empty directory.")
        stored = (self.state.get_project(self.pid) or {}).get("mode")
        self.mode = stored or mode
        info = self.git.prepare_workspace(
            self.config.project_dir, cfg.development_branch, cfg.default_branch, cfg.git_isolation,
            self.config.worktree_path(), self.mode, cfg.allow_git_init, BACKUPS_DIR / self.pid,
        )
        self.workspace = info.workspace_path
        if self.workspace not in self.policy.allowed_roots:
            self.policy.allowed_roots.append(self.workspace)
        operation = self.git.in_progress_operation(self.workspace)
        if operation:
            raise GitSafetyError(f"a git {operation} is in progress in the workspace")
        leftovers = self.git.uncommitted_changes(self.workspace)
        if leftovers:
            info.notes.append(f"workspace has {len(leftovers)} uncommitted change(s) from interrupted work; they are kept and reviewed with the resumed task")
        self.state.upsert_project(
            self.pid, name=cfg.project_name, path=str(self.config.project_dir), workspace_path=str(self.workspace), mode=self.mode,
            config_path=self.config.config_path, status="ACTIVE", base_branch=info.base_branch, dev_branch=info.dev_branch,
        )
        for note in info.notes:
            log.info("workspace: %s", note)
        self.events.publish("PROJECT_STAGE_CHANGED", f"workspace ready on {info.dev_branch} at {self.workspace}", status="OK", metadata={"notes": info.notes, "isolation": info.isolation, "user_uncommitted_changes": len(info.user_uncommitted_changes)})
        self._refresh_profile()
        self._setup_remote()

    # ============================================================ git push
    def _setup_remote(self) -> None:
        cfg = self.config.project
        if not cfg.git_push_enabled or not self.workspace:
            return
        try:
            message = self.git.ensure_remote(self.workspace, cfg.git_remote, cfg.git_remote_url)
            self._push_ready = True
            self.events.publish("PROJECT_STAGE_CHANGED", f"git push enabled: {message}; branch {cfg.development_branch} will be pushed after each checkpoint", status="OK")
        except GitError as exc:
            self._push_ready = False
            self.events.publish("GIT_PUSH_FAILED", f"git push disabled for this run: {exc}", status="ERROR")
            self.notifier.notify("SYSTEM_ERROR", "GitHub push not available", {"Remote": cfg.git_remote}, [str(exc)[:300]],
                                 footer="Set the repository URL in the project settings (dashboard) or add the remote yourself.")

    async def _push(self, context: str, task_id: int | None = None) -> None:
        cfg = self.config.project
        if not (cfg.git_push_enabled and self._push_ready and self.workspace):
            return
        async with self._serial:
            ok, message = await asyncio.to_thread(self.git.push, self.workspace, cfg.git_remote, cfg.development_branch)
        self.state.kv_set(f"last_push:{self.pid}", json.dumps({"ok": ok, "message": message[:500], "context": context, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))
        if ok:
            self.events.publish("GIT_PUSHED", f"{context}: {message}", status="OK", task_id=task_id)
        else:
            self.events.publish("GIT_PUSH_FAILED", f"{context}: {message[:400]}", status="ERROR", task_id=task_id)
            self.notifier.notify("SYSTEM_ERROR", "GitHub push failed", {"Branch": cfg.development_branch, "Remote": cfg.git_remote}, [message[:400]],
                                 footer="The commit is safe locally; the next checkpoint retries the push.", dedupe_key=f"push-failed-{fingerprint(message[:120])}")

    # ======================================================== usage limits
    async def _handle_usage_limit(self, exc: UsageLimitReached) -> None:
        """Wait for the Claude usage limit to reset (auto-resume) or stop the run cleanly."""
        if not self.config.project.auto_resume_on_limit:
            self.notifier.notify("SYSTEM_STOPPED", "Claude usage limit reached", {"Status": "STOPPED"}, [str(exc)],
                                 footer="State is saved. Start again after the limit resets, or enable auto-resume in the project settings.")
            raise StopRun(f"{exc} - run stopped (auto-resume is off)", "LIMIT_REACHED")
        now = time.time()
        if exc.resets_at and exc.resets_at > now:
            until = exc.resets_at + self.limit_margin_seconds
        else:
            until = now + self.limit_probe_seconds  # unknown reset time: probe again later
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
        self.state.kv_set(f"limit_wait:{self.pid}", json.dumps({"until": until, "reason": str(exc)}))
        self.state.update_project(self.pid, status="WAITING_FOR_LIMIT")
        self.events.publish("SYSTEM_PAUSED", f"{exc}; waiting until {when} and then resuming automatically", status="WAITING_FOR_LIMIT", metadata={"until": until})
        self.notifier.notify("SYSTEM_PAUSED", "Usage limit reached - will resume automatically", {"Resumes at": when}, [str(exc)], dedupe_key=f"limit-{int(until // 600)}")
        started_wait = time.monotonic()
        while time.time() < until and not self._stop_requested():
            self._apply_control_commands()
            if self.state.kv_get(f"limit_resume_now:{self.pid}") == "1":
                self.state.kv_set(f"limit_resume_now:{self.pid}", "0")
                break
            await asyncio.sleep(min(5.0, max(0.2, until - time.time())))
        self.started += time.monotonic() - started_wait  # waiting does not count against max_execution_minutes
        self.state.kv_set(f"limit_wait:{self.pid}", "")
        self.state.update_project(self.pid, status="ACTIVE")
        if not self._stop_requested():
            self.events.publish("SYSTEM_RESUMED", "resuming after the usage-limit wait", status="RUNNING")
            self.notifier.notify("SYSTEM_RESUMED", "Resumed after usage limit", {"Status": "RUNNING"})

    def _refresh_profile(self) -> ProjectProfile:
        assert self.workspace is not None
        profile = inspect_project(self.workspace, self.config.project.project_brief)
        self.profile = profile
        if profile.pyside_entry:
            self.policy.app_modules.add(profile.pyside_entry)
        if self.config.project.pyside.entry:
            entry = self.config.project.pyside.entry
            if entry.endswith(".py"):
                self.policy.app_scripts.append(str((self.workspace / entry).resolve()))
            else:
                self.policy.app_modules.add(entry)
        self.test_manager = TestManager(self.config, self.state, self.events, self.executor, profile)
        self.scanner = SecurityScanner(self.config, self.state, self.events, self.executor, profile)
        self.deployer = self.deployer or DeploymentManager(self.config, self.state, self.events, self.executor, profile)
        self.deployer.profile = profile
        context_parts = []
        for name in ("CLAUDE.md", "README.md"):
            path = self.workspace / name
            if path.exists():
                context_parts.append(f"### {name}\n" + path.read_text(encoding="utf-8", errors="replace")[:6000])
        self.runner.project_context = "\n\n".join(context_parts)
        return profile

    def _profile_summary(self) -> str:
        data = self.profile.model_dump() if self.profile else {}
        return json.dumps({k: data.get(k) for k in PROFILE_KEYS}, indent=1)

    # ============================================================ controls
    def _stop_requested(self) -> bool:
        if self.stop_reason:
            return True
        if self.state.get_flag(f"stop:{self.pid}") == "1":
            self.stop_reason = "stop requested"
            return True
        elapsed_min = (time.monotonic() - self.started) / 60
        if elapsed_min >= self.config.workflow.max_execution_minutes:
            self.stop_reason = f"maximum execution time of {self.config.workflow.max_execution_minutes} minutes reached"
            return True
        return False

    def _paused(self) -> bool:
        return self.state.get_flag(f"pause:{self.pid}") == "1"

    async def _wait_if_paused(self) -> None:
        if not self._paused():
            return
        if not self._paused_announced:
            self._paused_announced = True
            self.events.publish("SYSTEM_PAUSED", "execution paused; current safe operation finished", status="PAUSED")
            self.notifier.notify("SYSTEM_PAUSED", "System paused", {"Status": "PAUSED"}, ["No new work starts until /resume."])
        while self._paused() and not self._stop_requested():
            self._apply_control_commands()
            await asyncio.sleep(3)
        if not self._paused():
            self._paused_announced = False
            self.events.publish("SYSTEM_RESUMED", "execution resumed", status="RUNNING")
            self.notifier.notify("SYSTEM_RESUMED", "System resumed", {"Status": "RUNNING"})

    def _apply_control_commands(self) -> None:
        for command in self.state.pending_control_commands(self.pid):
            if command["command"] == "GENERATE_REPORT":
                try:
                    md, _, _ = generate_final_report(self.config, self.state, self._git_log())
                    ok, message = True, f"report written to {md}"
                    self.events.publish("REPORT_GENERATED", message, status="OK")
                except Exception as exc:
                    ok, message = False, f"report generation failed: {exc}"
            else:
                ok, message = self.tasks.apply_command(command["command"], command.get("args") or {}, command["requested_by"])
            self.state.mark_control_command(command["id"], "APPLIED" if ok else "REJECTED", message)
            log.info("control command %s: %s", command["command"], message)

    # ======================================================= project stages
    async def _project_stage(self, stage: Stage, coro_factory) -> Any:
        stage_run = self.state.start_stage_run(self.pid, None, stage.value)
        self.state.update_project(self.pid, current_stage=stage.value)
        self.events.publish("PROJECT_STAGE_CHANGED", f"project stage {stage.value}", status="IN_PROGRESS")
        try:
            result = await coro_factory()
        except BaseException as exc:
            self.state.finish_stage_run(stage_run, "FAILED", {"error": str(exc)[:1000]})
            raise
        self.state.finish_stage_run(stage_run, "COMPLETED", {})
        return result

    def _stage_done(self, stage: Stage) -> bool:
        row = self.state.last_stage_run(self.pid, None, stage.value)
        return bool(row and row["status"] == "COMPLETED")

    async def _project_stages(self) -> None:
        await self._project_stage(Stage.PROJECT_SCAN, self._scan)
        if self.mode == "new":
            await self._new_project_flow()
        else:
            if not self._stage_done(Stage.REQUIREMENT_ANALYSIS) or not self.state.list_requirements(self.pid):
                output = await self._project_stage(Stage.REQUIREMENT_ANALYSIS, self._requirement_analysis)
                await self._project_stage(Stage.TASK_PLANNING, lambda: self._task_planning(output))
            else:
                await self._project_stage(Stage.TASK_PLANNING, lambda: self._task_planning(None))

    async def _scan(self) -> None:
        profile = self._refresh_profile()
        self.state.update_project(self.pid, scan_summary=profile.model_dump(), app_types=profile.application_types)
        report = REPORTS_DIR / f"project-scan-{self.pid}.json"
        report.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        self.events.publish("PROJECT_SCANNED", f"detected {profile.application_types or ['nothing yet']} ({self.mode} mode), {profile.python_files} python files, {profile.test_files} test files", status="OK", metadata={"profile": profile.model_dump()})
        if self.mode == "existing" and self.state.kv_get(f"baseline_done:{self.pid}") != "1" and profile.python_files:
            assert self.scanner and self.test_manager and self.workspace
            await asyncio.to_thread(self.scanner.run_all, self.workspace, None, dependency_scan=True, baseline=True)
            layers = [l for l in self.test_manager.plan_layers(self.workspace, "BASELINE") if l.name in ("pytest", "qt")]
            await asyncio.to_thread(self.test_manager.run_suite, layers, self.workspace, None, "BASELINE")
            self.state.kv_set(f"baseline_done:{self.pid}", "1")

    def _baseline_summary(self) -> str:
        runs = [r for r in self.state.list_test_runs(self.pid, limit=50) if r["stage"] == "BASELINE"]
        findings = [f for f in self.state.open_security_findings(self.pid) if f["baseline"]]
        lines = [f"- baseline {r['test_type']}: {r['status']} ({r['summary']})" for r in runs[:4]]
        if findings:
            counts: dict[str, int] = {}
            for f in findings:
                counts[f["severity"]] = counts.get(f["severity"], 0) + 1
            lines.append(f"- pre-existing security findings: {counts}")
        return "\n".join(lines) or "- no baseline results"

    def _registry_text(self) -> str:
        reqs = self.state.list_requirements(self.pid)
        tasks = self.state.list_tasks(self.pid)
        req_lines = [f"- {r['code']} [{r['status']}] {r['title']}" for r in reqs] or ["- (empty)"]
        task_lines = [f"- #{t['id']} [{t['status']}] {t['title']}" for t in tasks] or ["- (empty)"]
        return "## Existing requirements registry\n" + "\n".join(req_lines) + "\n\n## Existing tasks\n" + "\n".join(task_lines)

    def _owner_input(self) -> str:
        """Project notes and every answered question round, passed to planning agents as authoritative input."""
        parts: list[str] = []
        notes = self.config.project.project_notes.strip()
        if notes:
            parts.append(f"## Project description from the owner\n{notes}")
        answered = [a for a in self.state.list_approvals(self.pid) if a["kind"] == "QUESTIONS" and a["status"] == "APPROVED"]
        for item in answered:
            questions = (item.get("details") or {}).get("questions", [])
            parts.append("## Questions you asked earlier\n" + "\n".join(f"- {q}" for q in questions) +
                         f"\n### Answer from the project owner (authoritative)\n{item.get('decision_note') or '(no answer text)'}")
        return ("\n\n".join(parts) + "\n\n") if parts else ""

    def _ask_questions(self, stage: Stage, questions: list[str], summary: str) -> ProjectInputRequired:
        questions = [q for q in questions if q.strip()] or [summary]
        round_no = sum(1 for a in self.state.list_approvals(self.pid) if a["kind"] == "QUESTIONS") + 1
        approval_id, _ = self.state.request_approval(self.pid, "QUESTIONS", f"{stage.value}:{round_no}", {"stage": stage.value, "questions": questions})
        self.notifier.notify("APPROVAL_REQUIRED", "The Product Manager has questions", {"Questions": len(questions)}, questions,
                             footer="Answer them in the dashboard (Control Panel > Questions), then resume.", dedupe_key=f"questions-{approval_id}")
        return ProjectInputRequired(f"The Product Manager needs answers to {len(questions)} question(s) (question set #{approval_id}). "
                                    "Answer them in the dashboard Control Panel, or with: python main.py answer " + str(approval_id))

    def _close_question_rounds(self, stage: Stage) -> None:
        for item in self.state.list_approvals(self.pid, "PENDING"):
            if item["kind"] == "QUESTIONS" and str(item["subject"]).startswith(stage.value + ":"):
                self.state.decide_approval(item["id"], False, "orchestrator", "superseded: the Product Manager no longer needs this information")

    async def _requirement_analysis(self):
        assert self.workspace and self.profile
        prompt = (
            "# Requirement analysis for an existing application\n\n"
            f"{self._owner_input()}"
            f"## Detected project profile\n```json\n{self._profile_summary()}\n```\n\n"
            f"## Baseline results (run by the orchestrator)\n{self._baseline_summary()}\n\n"
            f"{self._registry_text()}\n\n"
            "## Your job\nInspect the documentation and code. Describe the purpose and existing features, identify incomplete "
            "functionality and missing requirements, and produce requirements (with acceptance criteria) and small, prioritised "
            "development tasks for everything that is not yet implemented. Do not duplicate entries already in the registry. "
            "If baseline tests fail, include a high-priority task to fix them."
        )
        result = await self._agent("product_manager", prompt, None, Stage.REQUIREMENT_ANALYSIS.value, replacements={})
        if not result.output or result.status == Status.ERROR:
            raise StopRun(f"requirement analysis failed: {result.error}", "ERROR")
        if result.output.status == Status.BLOCKED:
            raise self._ask_questions(Stage.REQUIREMENT_ANALYSIS, result.output.questions, result.output.summary)
        self._close_question_rounds(Stage.REQUIREMENT_ANALYSIS)
        return result.output

    async def _task_planning(self, output) -> None:
        created: list[int] = []
        if output is not None:
            mapping = self.tasks.register_requirements(output)
            created += self.tasks.register_tasks(output, mapping)
        created += self.tasks.ensure_tasks_for_approved_requirements()
        created += self._ensure_security_remediation_task()
        proposed = self.state.list_requirements(self.pid, ["PROPOSED"])
        if proposed:
            for req in proposed:
                self.state.request_approval(self.pid, "REQUIREMENT", req["code"], {"title": req["title"], "description": req["description"]})
            self.notifier.notify(
                "APPROVAL_REQUIRED", "Requirements awaiting approval", {"Count": len(proposed)},
                [f"{r['code']}: {r['title']}" for r in proposed], footer="Approve with: python main.py approve-requirement REQ-xxx (or from the dashboard)",
                dedupe_key=fingerprint("proposed", ",".join(r["code"] for r in proposed)),
            )
        self.events.publish("PROJECT_STAGE_CHANGED", f"task planning: {len(created)} new task(s), {len(proposed)} requirement(s) awaiting approval", status="OK")

    def _ensure_security_remediation_task(self) -> list[int]:
        critical = [f for f in self.state.open_security_findings(self.pid) if f["baseline"] and f["severity"] == "CRITICAL"]
        if not critical or not self.config.security.block_preexisting_critical:
            return []
        task_id, created = self.state.create_task(
            self.pid, "Remediate pre-existing critical security findings",
            "Fix the CRITICAL findings reported by the baseline security scan:\n" + "\n".join(f"- #{f['id']} {f['tool_name']} {f['file_path']}:{f['line_number']} {f['description'][:200]}" for f in critical),
            None, 1, "security", [f"Finding #{f['id']} ({f['rule_id']}) is resolved and no longer reported by {f['tool_name']}" for f in critical],
            sorted({f["file_path"] for f in critical if f["file_path"]}), self.config.project.max_retries_per_task,
        )
        return [task_id] if created else []

    async def _new_project_flow(self) -> None:
        assert self.workspace
        brief = self.workspace / self.config.project.project_brief
        if not brief.exists() or "<What problem" in brief.read_text(encoding="utf-8"):
            if not brief.exists():
                brief.parent.mkdir(parents=True, exist_ok=True)
                brief.write_text(BRIEF_TEMPLATE, encoding="utf-8")
            raise ProjectInputRequired(f"Describe the new project in {brief} and run the controller again.")
        brief_text = brief.read_text(encoding="utf-8")[:20000]
        if self.config.project.require_project_discovery and not self._stage_done(Stage.PROJECT_DISCOVERY):
            async def discovery():
                prompt = (f"# Project discovery\n\n## Project brief ({self.config.project.project_brief})\n{brief_text}\n\n{self._owner_input()}"
                          "Check whether the brief together with the owner's answers contains enough information (purpose, users, features, workflows, "
                          "technology preferences, data, UI, integrations, deployment). If critical information is still missing return BLOCKED with "
                          "precise questions; never repeat a question the owner already answered. Do not invent business requirements.")
                result = await self._agent("product_manager", prompt, None, Stage.PROJECT_DISCOVERY.value)
                if not result.output or result.status == Status.ERROR:
                    raise StopRun(f"project discovery failed: {result.error}", "ERROR")
                if result.output.status == Status.BLOCKED or result.output.questions and result.output.status != Status.PASS:
                    questions_path = brief.parent / "open_questions.md"
                    questions_path.write_text("# Open questions from the Product Manager\n\nAnswer them in the dashboard (Control Panel > Questions) "
                                              "or in the project brief, then run the controller again.\n\n" + "\n".join(f"- {q}" for q in result.output.questions), encoding="utf-8")
                    raise self._ask_questions(Stage.PROJECT_DISCOVERY, result.output.questions, result.output.summary)
                self._close_question_rounds(Stage.PROJECT_DISCOVERY)
            await self._project_stage(Stage.PROJECT_DISCOVERY, discovery)

        if not self._stage_done(Stage.PROJECT_SPECIFICATION):
            async def specification():
                prompt = (f"# Project specification\n\n## Project brief\n{brief_text}\n\n{self._owner_input()}{self._registry_text()}\n\n"
                          "Write docs/PRD.md and return all requirements (source 'documented', source_reference the brief) with testable "
                          "acceptance criteria, plus development tasks ordered: core infrastructure, database, backend, API, UI, authentication, "
                          "business logic, integrations, reporting, extras. Do NOT include a project-initialisation task; the orchestrator adds it.")
                result = await self._agent("product_manager", prompt, None, Stage.PROJECT_SPECIFICATION.value)
                if not result.output or result.status != Status.PASS:
                    raise StopRun(f"project specification failed: {result.error or result.output.summary if result.output else result.error}", "ERROR")
                self.state.kv_set(f"spec_output:{self.pid}", result.output.model_dump_json())
            await self._project_stage(Stage.PROJECT_SPECIFICATION, specification)

        if self.config.project.require_architecture_design and not self._stage_done(Stage.PROJECT_ARCHITECTURE):
            async def architecture():
                approval = self.state.approval_status(self.pid, "TECH_STACK", "project:architecture")
                if approval == "PENDING":
                    raise ProjectInputRequired("The proposed technology stack awaits approval (python main.py approvals).")
                if approval == "REJECTED":
                    raise ProjectInputRequired("The proposed technology stack was rejected; update the brief's technology preferences and reset the PROJECT_ARCHITECTURE stage.")
                if approval == "APPROVED":
                    return
                prompt = (f"# Architecture for a new project\n\n## Project brief\n{brief_text}\n\n{self._owner_input()}Read docs/PRD.md. Write docs/architecture.md as described in your instructions. "
                          "If the brief states no technology preference, recommend a stack with reasons and set requires_human_approval.")
                result = await self._agent("architect", prompt, None, Stage.PROJECT_ARCHITECTURE.value)
                if not result.output or result.status == Status.ERROR:
                    raise StopRun(f"architecture design failed: {result.error}", "ERROR")
                if result.output.requires_human_approval:
                    approval_id, _ = self.state.request_approval(self.pid, "TECH_STACK", "project:architecture", {"stack": result.output.technology_stack, "reason": result.output.approval_reason, "approach": result.output.approach[:2000]})
                    raise ProjectInputRequired(f"Technology stack {result.output.technology_stack} needs approval (approval #{approval_id}): python main.py approve {approval_id}")
            await self._project_stage(Stage.PROJECT_ARCHITECTURE, architecture)

        spec_json = self.state.kv_get(f"spec_output:{self.pid}")
        if not self._stage_done(Stage.TASK_PLANNING) and spec_json:
            from agent_models import ProductManagerOutput

            output = ProductManagerOutput.model_validate_json(spec_json)

            async def planning():
                init_id, _ = self.state.create_task(
                    self.pid, "Initialize project structure",
                    "Create the project skeleton following docs/architecture.md: source package, configuration with an environment "
                    "variable template (.env.example, no real secrets), database models and initial migration, logging, error handling, "
                    "dependency file, README with run instructions, a health check (web) or main window (desktop), and initial automated tests.",
                    None, 1, "infrastructure",
                    ["The application starts locally without errors", "Configuration is read from environment variables; .env.example documents them",
                     "Logging and error handling are in place", "README explains setup and how to run tests", "Initial automated tests pass"],
                    ["**"], self.config.project.max_retries_per_task,
                )
                await self._task_planning(output)
                for task in self.state.list_tasks(self.pid):
                    if task["id"] != init_id:
                        self.state.add_dependency(task["id"], init_id)
            await self._project_stage(Stage.TASK_PLANNING, planning)
            await self._commit_project_documents()
        else:
            await self._project_stage(Stage.TASK_PLANNING, lambda: self._task_planning(None))

    async def _commit_project_documents(self) -> None:
        """New projects: the brief, PRD and architecture become their own first commit before any code."""
        assert self.workspace
        protected = {self.config.project.default_branch, "main", "master"}
        try:
            async with self._serial:
                sha = await asyncio.to_thread(
                    self.git.commit_changes, self.workspace, "docs: add project brief, specification and architecture", None, protected,
                    ["Planning documents produced by the Product Manager and Architect agents before implementation."],
                )
        except GitError as exc:
            self.events.publish("SYSTEM_ERROR", f"could not commit planning documents: {exc}", status="ERROR")
            return
        if sha:
            self.state.add_checkpoint(self.pid, None, sha, self.config.project.development_branch, "planning documents")
            self.events.publish("CHECKPOINT_CREATED", f"planning documents committed ({sha[:10]})", status="OK", metadata={"commit": sha})
            await self._push("planning documents")

    # ============================================================ task loop
    async def _task_loop(self, max_tasks: int) -> str:
        running: dict[int, asyncio.Task] = {}
        started = 0
        self.state.update_project(self.pid, current_stage="TASK_EXECUTION")
        while True:
            self._apply_control_commands()
            if self._stop_requested():
                break
            await self._wait_if_paused()
            if self._stop_requested():
                break
            while len(running) < self.config.project.max_parallel_tasks and started < max_tasks:
                candidates = [t for t in self.state.runnable_tasks(self.pid, self.owner) if t["id"] not in running]
                picked = None
                for task in candidates:
                    if self.state.acquire_task_lease(task["id"], self.owner, self.config.workflow.lease_seconds):
                        picked = task
                        break
                if not picked:
                    break
                started += 1
                self._active.add(picked["id"])
                running[picked["id"]] = asyncio.create_task(self._run_task(picked["id"]))
            if not running:
                if started >= max_tasks:
                    self.stop_reason = f"maximum tasks per run ({max_tasks}) reached"
                    break
                if await self._plan_more_work():
                    continue
                if self.config.project.stop_after == "manual":
                    await self._idle_until_work_or_stop()
                    if self._stop_requested():
                        break
                    continue
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                task_id = next(k for k, v in running.items() if v is finished)
                running.pop(task_id)
                self._active.discard(task_id)
                exc = finished.exception()
                if isinstance(exc, StopRun):
                    self.stop_reason = exc.reason
                    self.stop_status = exc.status
                elif exc:
                    log.error("task %s crashed: %s", task_id, exc)
            if self.config.project.stop_after == "task" and self.tasks_completed >= 1 and not self.stop_reason:
                self.stop_reason = "stop_after=task: a task was completed; start again for the next one"
                break
        if running:
            await asyncio.gather(*running.values(), return_exceptions=True)
        return self.stop_reason or self._no_work_reason()

    async def _idle_until_work_or_stop(self, poll_seconds: float = 5.0) -> None:
        """stop_after=manual: keep the controller alive, waiting for approvals, retries or new work."""
        self.state.update_project(self.pid, current_stage="IDLE_WAITING_FOR_WORK")
        self.events.publish("SYSTEM_PAUSED", "no runnable work: waiting for approvals, retries or new requirements (stop_after=manual)", status="IDLE")
        self.notifier.notify("SYSTEM_PAUSED", "All current work finished - waiting", {"Mode": "runs until you stop it"},
                             ["Approve requirements, retry blocked tasks or answer questions to give it more work."], dedupe_key=f"idle-{self.run_id}")
        while not self._stop_requested():
            self._apply_control_commands()
            if self.state.runnable_tasks(self.pid, self.owner) or self.tasks.ensure_tasks_for_approved_requirements():
                self.state.update_project(self.pid, current_stage="TASK_EXECUTION")
                self.events.publish("SYSTEM_RESUMED", "new work available; continuing", status="RUNNING")
                return
            await asyncio.sleep(poll_seconds)

    async def _plan_more_work(self) -> bool:
        created = self.tasks.ensure_tasks_for_approved_requirements()
        if created:
            return True
        remaining = [t for t in self.state.list_tasks(self.pid) if t["status"] in ("PENDING", "IN_PROGRESS")]
        if remaining or self._reanalysed or self.mode == "new":
            return False
        incomplete = [r for r in self.state.list_requirements(self.pid, ["APPROVED", "IN_PROGRESS"])]
        if not incomplete:
            return False
        self._reanalysed = True
        output = await self._project_stage(Stage.REQUIREMENT_ANALYSIS, self._requirement_analysis)
        before = len(self.state.list_tasks(self.pid))
        await self._project_stage(Stage.TASK_PLANNING, lambda: self._task_planning(output))
        return len(self.state.list_tasks(self.pid)) > before

    async def _run_task(self, task_id: int) -> None:
        task = self.state.get_task(task_id)
        assert task is not None
        try:
            if task["status"] == "PENDING":
                task = self.state.transition_task(task_id, to_status="IN_PROGRESS", reason="selected by orchestrator")
                self.events.publish("TASK_STARTED", f"#{task_id} {task['title']}", status="IN_PROGRESS", task_id=task_id)
                self.notifier.notify("TASK_STARTED", "Autonomous Development Started", {"Task": task["title"], "Stage": task["stage"], "Status": "IN PROGRESS"}, dedupe_key=f"task-started-{task_id}-{task['retry_count']}")
            else:
                self.events.publish("TASK_STARTED", f"#{task_id} resumed at {task['stage']}", status="IN_PROGRESS", task_id=task_id, metadata={"resumed": True})
            if task.get("requirement_id"):
                req = self.state.get_requirement(task["requirement_id"])
                if req and req["status"] == "APPROVED":
                    self.state.update_requirement(req["id"], status="IN_PROGRESS")
            while True:
                task = self.state.get_task(task_id)
                assert task is not None
                if task["cancel_requested"]:
                    self.state.transition_task(task_id, to_status="CANCELLED", reason="cancellation requested")
                    self.events.publish("TASK_CANCELLED", f"#{task_id} cancelled", status="CANCELLED", task_id=task_id)
                    return
                if task["status"] != "IN_PROGRESS" or task["stage"] == Stage.COMPLETED.value:
                    return
                if self._stop_requested():
                    return
                await self._wait_if_paused()
                if self._stop_requested():
                    return
                stage = Stage(task["stage"])
                try:
                    outcome = await self._execute_stage(task, stage)
                except UsageLimitReached as exc:
                    await self._handle_usage_limit(exc)  # waits (auto-resume) or raises StopRun
                    continue  # re-run the interrupted stage
                if outcome.block_reason:
                    self._block(task, outcome.block_reason)
                    return
                if outcome.error:
                    errors = task["stage_error_count"] + 1
                    self.state.update_task_fields(task_id, stage_error_count=errors)
                    self.notifier.notify("AGENT_CRASH", "Agent or stage error", {"Task": task["title"], "Stage": stage.value, "Attempt": f"{errors}/{self.config.workflow.max_stage_errors}"}, [outcome.reason[:300]])
                    if errors >= self.config.workflow.max_stage_errors:
                        self._block(task, f"{stage.value} failed {errors} times: {outcome.reason}")
                        return
                    continue
                assert outcome.next_stage is not None
                self._transition(task, stage, outcome)
                if outcome.next_stage == Stage.COMPLETED:
                    self._complete(task_id)
                    return
        except BudgetExceeded as exc:
            self.stop_reason = f"API budget exhausted: {exc}"
            self.notifier.notify("SYSTEM_STOPPED", "Budget limit reached", {"Task": task["title"]}, [str(exc)], footer="Raise max_budget_usd in workflow.json to continue.")
            raise StopRun(self.stop_reason) from exc
        except TurnLimitExceeded as exc:
            self._block(self.state.get_task(task_id), str(exc))
        except FatalAgentError as exc:
            self.state.set_flag(f"pause:{self.pid}", "1", "orchestrator")
            self.notifier.notify("AGENT_CRASH", "Agent crash - system paused", {"Task": task["title"]}, [redact(str(exc))[:400]], footer="Fix authentication/billing, then /resume and start the controller.")
            raise StopRun(f"fatal agent error: {exc}", "ERROR") from exc
        except GitSafetyError as exc:
            self._block(self.state.get_task(task_id), f"git safety: {exc}")
        finally:
            self.state.release_task_lease(task_id, self.owner)
            self.state.release_files(self.pid, task_id)

    def _transition(self, task: dict[str, Any], current: Stage, outcome: StageOutcome) -> None:
        target = outcome.next_stage
        assert target is not None
        allowed = set(TASK_TRANSITIONS.get(current, set()))
        if not self.config.project.require_independent_review:
            if current in (S.DEVELOPMENT, S.BUG_FIXING):
                allowed.add(S.QA_TESTING)
        if target not in allowed:
            raise RuntimeError(f"illegal transition {current.value} -> {target.value} for task {task['id']}")
        self.state.transition_task(task["id"], to_stage=target.value, reason=outcome.reason[:1000], stage_error_count=0)
        self.events.publish("TASK_STAGE_CHANGED", f"#{task['id']} {current.value} -> {target.value}: {outcome.reason[:200]}", status=target.value, task_id=task["id"], metadata={"from": current.value, "to": target.value, "repair": outcome.repair})

    def _block(self, task: dict[str, Any] | None, reason: str) -> None:
        if not task:
            return
        self.state.transition_task(task["id"], to_status="BLOCKED", reason=reason, blocked_reason=reason[:2000])
        self.tasks_blocked += 1
        if self.run_id:
            self.state.increment_run(self.run_id, "tasks_blocked")
        issues = self.state.open_issues(task["id"])
        self.events.publish("TASK_BLOCKED", f"#{task['id']} {task['title']}: {reason[:300]}", status="BLOCKED", task_id=task["id"])
        self.notifier.notify(
            "TASK_BLOCKED", "Task Blocked - human intervention required",
            {"Task": task["title"], "Stage": task["stage"], "Repair attempts": f"{task['retry_count']}/{task['max_retries']}", "Reason": reason[:500]},
            [f"[{i['severity']}] {i['title']}" for i in issues[:8]],
            footer="Other independent tasks continue. Retry with: python main.py retry-task " + str(task["id"]),
        )
        write_task_report(self.config, self.state, task["id"])

    def _complete(self, task_id: int) -> None:
        task = self.state.transition_task(task_id, to_status="COMPLETED", to_stage=Stage.COMPLETED.value, reason="all stages passed")
        self.tasks_completed += 1
        if self.run_id:
            self.state.increment_run(self.run_id, "tasks_completed")
        evidence = (task.get("design") or {}).get("validation_evidence", {})
        if task.get("requirement_id") and self.tasks.requirement_done(task["requirement_id"]):
            all_evidence = [
                (t.get("design") or {}).get("validation_evidence")
                for t in self.state.query("SELECT * FROM tasks WHERE requirement_id = ?", (task["requirement_id"],))
            ]
            all_evidence = [e for e in all_evidence if e]
            if all_evidence:
                self.state.update_requirement(task["requirement_id"], status="COMPLETED", validation_evidence={"tasks": all_evidence})
                req = self.state.get_requirement(task["requirement_id"])
                self.events.publish("REQUIREMENT_COMPLETED", f"{req['code']} {req['title']} completed with validation evidence", status="COMPLETED", task_id=task_id)
            else:
                self.state.update_requirement(task["requirement_id"], status="IMPLEMENTED")
        self.events.publish("TASK_COMPLETED", f"#{task_id} {task['title']}", status="COMPLETED", task_id=task_id, metadata={"evidence": evidence})
        self.notifier.notify(
            "TASK_COMPLETED", "Task Completed",
            {"Task": task["title"], "Development": "PASS", "Code Review": "PASS", "QA": "PASS", "Security": "PASS", "Staging": "PASS",
             "Validation": "PASS" if evidence else "NOT RUN", "Commit": (evidence or {}).get("commit", "")[:10]},
            dedupe_key=f"task-completed-{task_id}",
        )
        write_task_report(self.config, self.state, task_id)

    async def _finish_project_if_done(self) -> None:
        tasks = self.state.list_tasks(self.pid)
        if not tasks:
            return
        if any(t["status"] in ("PENDING", "IN_PROGRESS") for t in tasks) and self.stop_reason:
            return
        md, _, criteria = generate_final_report(self.config, self.state, self._git_log())
        self.events.publish("REPORT_GENERATED", f"final report: {md.name}", status="OK", metadata={"path": str(md), "criteria": criteria})
        blocked = [t for t in tasks if t["status"] == "BLOCKED"]
        on_hold = [t for t in tasks if t["status"] == "ON_HOLD"]
        if criteria["project_complete"]:
            self.state.update_project(self.pid, status="COMPLETED", current_stage="COMPLETED")
            self.events.publish("PROJECT_COMPLETED", "all mandatory requirements implemented and validated", status="COMPLETED")
            title = "Project Completed"
        else:
            title = "Run finished - project not complete"
        completed = [t for t in tasks if t["status"] == "COMPLETED"]
        self.notifier.notify(
            "PROJECT_COMPLETED", title,
            {"Tasks completed": f"{len(completed)}/{len(tasks)}", "Blocked": len(blocked), "Awaiting approval": len(on_hold), "Report": md.name},
            [f"{k.replace('_', ' ')}: {'yes' if v else 'NO'}" for k, v in criteria.items() if k != "project_complete"],
            dedupe_key=fingerprint("final", md.name),
        )

    def _git_log(self) -> list[dict[str, str]]:
        try:
            return self.git.log(self.workspace, 30) if self.workspace else []
        except GitError:
            return []

    # ======================================================== stage dispatch
    async def _execute_stage(self, task: dict[str, Any], stage: Stage) -> StageOutcome:
        handlers = {
            S.ARCHITECTURE_DESIGN: self._stage_architecture,
            S.DEVELOPMENT: self._stage_development,
            S.CODE_REVIEW: self._stage_code_review,
            S.QA_TESTING: self._stage_qa,
            S.SECURITY_TESTING: self._stage_security,
            S.BUG_FIXING: self._stage_bug_fixing,
            S.LOCAL_STAGING_DEPLOYMENT: self._stage_staging,
            S.PRODUCT_VALIDATION: self._stage_validation,
            S.GIT_CHECKPOINT: self._stage_checkpoint,
        }
        handler = handlers.get(stage)
        if handler is None:
            return StageOutcome(block_reason=f"task is in non-executable stage {stage.value}")
        attempt = self.state.count_stage_runs(task["id"], stage.value) + 1
        interrupted = self.state.last_stage_run(self.pid, task["id"], stage.value)
        was_interrupted = bool(interrupted and interrupted["status"] == "INTERRUPTED")
        stage_run = self.state.start_stage_run(self.pid, task["id"], stage.value, attempt)
        self._refresh_profile()
        try:
            outcome = await handler(task, was_interrupted)
        except (BudgetExceeded, TurnLimitExceeded, FatalAgentError, StopRun, GitSafetyError, UsageLimitReached):
            self.state.finish_stage_run(stage_run, "INTERRUPTED", {"reason": "run stopped"})
            raise
        except Exception as exc:
            log.exception("stage %s failed for task %s", stage.value, task["id"])
            self.events.publish("SYSTEM_ERROR", f"{stage.value} raised {type(exc).__name__}: {redact(str(exc))[:300]}", status="ERROR", task_id=task["id"])
            outcome = StageOutcome(error=True, reason=f"{type(exc).__name__}: {exc}")
        status = "FAILED" if outcome.error or outcome.block_reason else "COMPLETED"
        self.state.finish_stage_run(stage_run, status, {"next": outcome.next_stage.value if outcome.next_stage else None, "reason": outcome.reason[:1000], "block": outcome.block_reason})
        return outcome

    # ============================================================ helpers
    async def _agent(self, name: str, prompt: str, task: dict[str, Any] | None, stage: str, replacements: dict[str, list[str]] | None = None, read_dirs: list[Path] | None = None) -> AgentResult:
        assert self.workspace is not None
        if task:
            self.state.update_task_fields(task["id"], assigned_agent=name)
        request = AgentRequest(name, prompt, stage, task["id"] if task else None, replacements or {}, read_dirs or [], input_summary=prompt[:600])
        return await self.runner.run(request, self.workspace)

    def _task_context(self, task: dict[str, Any]) -> str:
        req = self.state.get_requirement(task["requirement_id"]) if task.get("requirement_id") else None
        criteria = task.get("acceptance_criteria") or (req or {}).get("acceptance_criteria") or []
        lines = [f"# Task #{task['id']}: {task['title']}", f"Kind: {task['kind']} | Priority: {task['priority']} | Repair attempt: {task['retry_count']}/{task['max_retries']}"]
        if req:
            lines.append(f"Requirement {req['code']}: {req['title']} - {req['description']}")
        lines += ["", task.get("description") or "", "", "## Acceptance criteria"] + [f"{i + 1}. {c}" for i, c in enumerate(criteria)]
        lines += ["", f"## Project profile\n```json\n{self._profile_summary()}\n```"]
        return "\n".join(lines)

    def _criteria(self, task: dict[str, Any]) -> list[str]:
        req = self.state.get_requirement(task["requirement_id"]) if task.get("requirement_id") else None
        return list(task.get("acceptance_criteria") or (req or {}).get("acceptance_criteria") or [])

    def _repair(self, task: dict[str, Any], target: Stage, reason: str) -> StageOutcome:
        fresh = self.state.get_task(task["id"]) or task
        if fresh["retry_count"] >= fresh["max_retries"]:
            return StageOutcome(block_reason=f"maximum repair attempts ({fresh['max_retries']}) reached. Last failure: {reason}")
        self.state.update_task_fields(task["id"], retry_count=fresh["retry_count"] + 1)
        return StageOutcome(target, f"repair {fresh['retry_count'] + 1}/{fresh['max_retries']}: {reason}", repair=True)

    def _task_scope(self, task: dict[str, Any]) -> list[str]:
        if self.config.project.max_parallel_tasks <= 1:
            return ["**"]
        return list(task.get("file_scope") or []) or ["**"]

    def _design(self, task: dict[str, Any]) -> dict[str, Any]:
        return dict(task.get("design") or {})

    def _save_design(self, task_id: int, **updates: Any) -> dict[str, Any]:
        task = self.state.get_task(task_id) or {}
        design = dict(task.get("design") or {})
        design.update(updates)
        self.state.update_task_fields(task_id, design=design)
        return design

    def _changed_files(self, task: dict[str, Any]) -> list[str]:
        assert self.workspace
        return self.git.changed_files(self.workspace, task.get("base_commit"))

    def _compile_check(self, files: list[str]) -> str | None:
        assert self.workspace
        py_files = [f for f in files if f.endswith(".py") and (self.workspace / f).exists()]
        if not py_files:
            return None
        result = self.executor.run([self.python, "-m", "compileall", "-q", *py_files], self.workspace, timeout=120)
        return None if result.ok else result.summary(2000)

    def _write_report(self, folder: str, name: str, data: dict[str, Any]) -> Path:
        path = REPORTS_DIR / folder / self.pid / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    def _record_findings_as_issues(self, task: dict[str, Any], findings, category: str, source: str, blocking_only: bool = False) -> list[int]:
        ids = []
        for finding in findings:
            severity = finding.severity.value if hasattr(finding.severity, "value") else str(finding.severity)
            blocking = severity_rank(severity) >= severity_rank("HIGH")
            if blocking_only and not blocking:
                continue
            issue_id, created = self.state.add_issue(
                self.pid, task["id"], finding.title, severity, category, source,
                finding.explanation + (f"\nEvidence: {finding.evidence}" if getattr(finding, "evidence", "") else ""), finding.file_path, finding.line_number, finding.recommendation,
            )
            ids.append(issue_id)
            if created:
                self.events.publish("BUG_CREATED", f"[{severity}] {finding.title} ({finding.file_path}:{finding.line_number})", status="OPEN", task_id=task["id"], agent=source, metadata={"issue_id": issue_id, "category": category})
        return ids

    # ===================================================== stage: ARCHITECTURE
    async def _stage_architecture(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        design = self._design(task)
        subject = f"task:{task['id']}"
        if design.get("approach") and self.state.approval_status(self.pid, "ARCHITECTURE", subject) == "APPROVED":
            return StageOutcome(S.DEVELOPMENT, "architecture approved by a human")
        if not self.config.project.require_architecture_design and design.get("approach") is None:
            self._save_design(task["id"], approach="(architecture design disabled by configuration)")
            return StageOutcome(S.DEVELOPMENT, "architecture design not required")
        prompt = (f"{self._task_context(task)}\n\n## Your job\nDesign the implementation of this task for the existing codebase. "
                  f"Inspect the relevant code first. List every file to create or modify (relative paths, including tests under `{self.config.project.test_dir}/`). "
                  "Respect the existing technology stack and conventions; avoid rewriting working code. Flag changes that need human approval.")
        result = await self._agent("architect", prompt, task, S.ARCHITECTURE_DESIGN.value)
        if result.status == Status.ERROR or not result.output:
            return StageOutcome(error=True, reason=f"architect error: {result.error}")
        out = result.output
        scope = sorted({p.replace("\\", "/").strip("/") for p in out.files_to_create + out.files_to_modify if p} | set(task.get("file_scope") or []))
        self._save_design(task["id"], approach=out.approach or out.summary, architecture_summary=out.architecture_summary, files=scope,
                          database_changes=out.database_changes, api_changes=out.api_changes, dependencies=out.dependencies_to_add,
                          risks=out.risks, security=out.security_considerations, test_plan=out.test_plan)
        self.state.update_task_fields(task["id"], file_scope=scope)
        self._write_report("reviews", f"task-{task['id']}-design.json", out.model_dump())
        if out.status == Status.FAIL:
            return StageOutcome(block_reason=f"architect: task cannot be implemented as described - {out.summary}")
        if out.requires_human_approval or out.status == Status.BLOCKED:
            approval_id, status = self.state.request_approval(self.pid, "ARCHITECTURE", subject, {"reason": out.approval_reason, "approach": out.approach[:3000], "dependencies": out.dependencies_to_add})
            if status != "APPROVED":
                self.notifier.notify("APPROVAL_REQUIRED", "Design needs approval", {"Task": task["title"], "Approval": f"#{approval_id}"}, [out.approval_reason or out.summary], footer=f"python main.py approve {approval_id}")
                return StageOutcome(block_reason=f"design requires human approval (approval #{approval_id}): {out.approval_reason or out.summary}")
        return StageOutcome(S.DEVELOPMENT, f"design ready: {len(scope)} file(s) in scope")

    # ====================================================== stage: DEVELOPMENT
    async def _stage_development(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.workspace
        if not task.get("base_commit"):
            self.state.update_task_fields(task["id"], base_commit=self.git.head_commit(self.workspace))
            task = self.state.get_task(task["id"]) or task
        scope = self._task_scope(task)
        if self.config.project.max_parallel_tasks > 1:
            ok, conflicts = self.state.reserve_files(self.pid, task["id"], scope)
            if not ok:
                return StageOutcome(error=True, reason=f"files reserved by another task: {conflicts}")
        design = self._design(task)
        previous = self.state.open_issues(task["id"], ["development"])
        notes = ""
        if interrupted:
            notes += "\n\nNOTE: a previous development attempt was interrupted. Inspect the current state of the files (some changes may already exist) and complete the work.\n"
        if previous:
            notes += "\n\n## Problems with the previous attempt\n" + "\n".join(f"- {i['title']}: {i['description'][:500]}" for i in previous)
        prompt = (f"{self._task_context(task)}\n\n## Design from the Architect\n```json\n{json.dumps({k: design.get(k) for k in ('approach', 'files', 'database_changes', 'api_changes', 'dependencies', 'security', 'test_plan')}, indent=1)[:12000]}\n```\n"
                  f"{notes}\n## Your job\nImplement this task now, including unit tests under `{self.config.project.test_dir}/`. "
                  f"You may check your work with `python -m pytest {self.config.project.test_dir} -q -m \"not e2e and not gui and not acceptance\"`.")
        result = await self._agent("developer", prompt, task, S.DEVELOPMENT.value, replacements={"task_scope": scope})
        if result.status == Status.ERROR or not result.output:
            return StageOutcome(error=True, reason=f"developer error: {result.error}")
        out = result.output
        if out.status == Status.BLOCKED:
            return StageOutcome(block_reason=f"developer blocked: {out.summary}")
        if previous:
            self.state.set_issue_status([i["id"] for i in previous], "FIXED_PENDING_VERIFICATION", "re-implemented")
        changed = self._changed_files(task)
        self._save_design(task["id"], developer_summary=out.summary, claimed_files=out.modified_files, actual_changed_files=changed)
        if out.status == Status.FAIL or not changed:
            reason = out.summary if out.status == Status.FAIL else "developer reported success but no file changes were detected"
            self.state.add_issue(self.pid, task["id"], "Implementation incomplete", "HIGH", "development", "orchestrator", reason)
            return self._repair(task, S.DEVELOPMENT, reason)
        compile_error = self._compile_check(changed)
        if compile_error:
            self.state.add_issue(self.pid, task["id"], "Python files do not compile", "HIGH", "build", "orchestrator", compile_error)
            return self._repair(task, S.BUG_FIXING, "changed files fail to compile")
        self.notifier.notify("DEVELOPMENT_PROGRESS", "Development finished", {"Task": task["title"], "Files changed": len(changed), "Agent": "Developer"}, changed[:10], dedupe_key=f"dev-{task['id']}-{task['retry_count']}")
        target = S.CODE_REVIEW if self.config.project.require_independent_review else S.QA_TESTING
        return StageOutcome(target, f"implemented; {len(changed)} file(s) changed")

    # ====================================================== stage: CODE REVIEW
    async def _stage_code_review(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.workspace
        diff = self.git.diff(self.workspace, task.get("base_commit"))
        if not diff.strip():
            return self._repair(task, S.DEVELOPMENT, "nothing to review: the change is empty")
        prompt = (f"{self._task_context(task)}\n\n## Change to review (git diff against the task's base commit)\n```diff\n{diff}\n```\n\n"
                  "Review this change independently. Check every acceptance criterion.")
        result = await self._agent("code_reviewer", prompt, task, S.CODE_REVIEW.value)
        if result.status == Status.ERROR or not result.output:
            return StageOutcome(error=True, reason=f"reviewer error: {result.error}")
        out = result.output
        attempt = self.state.count_stage_runs(task["id"], S.CODE_REVIEW.value)
        self._write_report("reviews", f"task-{task['id']}-review-{attempt}.json", out.model_dump())
        blocking = [f for f in out.findings if f.severity in (Severity.CRITICAL, Severity.HIGH)]
        unmet = [c for c in out.acceptance_criteria_met if not c.met]
        failed = out.status != Status.PASS or bool(blocking) or bool(unmet)
        self.events.publish("REVIEW_COMPLETED", f"#{task['id']} review {'FAIL' if failed else 'PASS'}: {len(out.findings)} finding(s), {len(unmet)} unmet criteria", status="FAIL" if failed else "PASS", task_id=task["id"], agent="code_reviewer")
        if not failed:
            self.state.verify_pending_issues(task["id"], ["review", "build", "development"])
            # Non-blocking findings stay in the review report (reports/reviews/) as advice.
            return StageOutcome(S.QA_TESTING, f"code review passed ({len(out.findings)} non-blocking finding(s))")
        self._record_findings_as_issues(task, blocking or out.findings, "review", "code_reviewer")
        for criterion in unmet:
            self.state.add_issue(self.pid, task["id"], f"Acceptance criterion not met: {criterion.criterion[:200]}", "HIGH", "review", "code_reviewer", criterion.evidence)
        self.notifier.notify("CODE_REVIEW_FAILED", "Code Review Failed", {"Task": task["title"], "Findings": len(out.findings), "Unmet criteria": len(unmet)},
                             [f"[{f.severity.value}] {f.title} ({f.file_path}:{f.line_number})" for f in (blocking or out.findings)[:8]], footer="Bug Fixer has been assigned.")
        return self._repair(task, S.BUG_FIXING, f"code review failed: {out.summary[:300]}")

    # ============================================================= stage: QA
    async def _start_qa_server(self, task: dict[str, Any]) -> DeploymentResult | None:
        assert self.profile and self.deployer and self.workspace
        has_ui = self.config.project.flask.has_browser_ui if self.config.project.flask.has_browser_ui is not None else self.profile.has_browser_ui
        if "flask" in self.profile.application_types and has_ui and self.config.testing.flask.playwright:
            return await self.deployer.deploy_flask(self.workspace, task["id"], purpose="qa")
        return None

    async def _run_tests(self, task: dict[str, Any], stage: str, base_url: str | None) -> TestSuiteResult:
        assert self.test_manager and self.workspace
        layers = self.test_manager.plan_layers(self.workspace, stage, base_url)
        async with self._serial:
            return await asyncio.to_thread(self.test_manager.run_suite, layers, self.workspace, task["id"], stage)

    def _suite_text(self, suite: TestSuiteResult) -> str:
        lines = [f"- {l.layer} ({l.tool}): {l.status} - {l.summary}" for l in suite.layers]
        for failure in suite.failures()[:25]:
            lines.append(f"  * {failure['nodeid']} [{failure['status']}]: {failure['message'][:1200]}")
        return "\n".join(lines)

    async def _stage_qa(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.workspace and self.test_manager and self.profile
        first_pass = self.state.count_stage_runs(task["id"], S.QA_TESTING.value) <= 1 or interrupted
        layers_preview = self.test_manager.plan_layers(self.workspace, "QA_TESTING", "http://127.0.0.1:<qa-port>")
        layer_text = "\n".join(f"- {l.name} ({l.tool}): marker expression `{l.marker_expr}`" + (f" - BLOCKED here: {l.blocked_reason}" if l.blocked_reason and "URL" not in l.blocked_reason else "") for l in layers_preview)
        if first_pass:
            diff = self.git.diff(self.workspace, task.get("base_commit"), max_chars=60_000)
            prompt = (f"{self._task_context(task)}\n\n## Change under test\n```diff\n{diff}\n```\n\n## Testing layers the orchestrator will run\n{layer_text}\n"
                      "- Browser tests: mark with @pytest.mark.e2e, use the `page` fixture with relative URLs; the orchestrator starts the app and passes --base-url.\n"
                      "- Qt tests: mark with @pytest.mark.qt; they run with QT_QPA_PLATFORM=offscreen.\n"
                      f"- Register custom markers in `{self.config.project.test_dir}/conftest.py` (pytest_configure + config.addinivalue_line) if they are not registered yet.\n"
                      "- The orchestrator sets the application's database environment variable to an isolated test database.\n\n"
                      "## Your job\nWrite or update automated tests covering every acceptance criterion (positive, negative, edge cases, regression). "
                      "Do not modify application code. Map each criterion to test ids in test_plan.")
            result = await self._agent("qa_engineer", prompt, task, S.QA_TESTING.value)
            if result.status == Status.ERROR or not result.output:
                return StageOutcome(error=True, reason=f"QA agent error: {result.error}")
            self._save_design(task["id"], qa_test_plan=[p.model_dump() for p in result.output.test_plan], qa_coverage_gaps=result.output.coverage_gaps)

        suite = await self._run_tests_with_server(task, "QA_TESTING")
        fix_rounds = 0
        while suite.status in ("FAIL", "ERROR"):
            artifacts = sorted({a for l in suite.layers for a in l.artifacts})[:40]
            read_dirs = [Path(l.artifacts_dir) for l in suite.layers if l.artifacts_dir]
            can_fix = fix_rounds < self.config.workflow.qa_test_fix_attempts
            prompt = (f"{self._task_context(task)}\n\n## Authoritative test results (executed by the orchestrator)\n{self._suite_text(suite)}\n\n"
                      "## Evidence artifacts - inspect them before concluding\n" + ("\n".join(f"- {a}" for a in artifacts) or "- (none)") + "\n"
                      + "".join(f"- pytest log: {Path(l.artifacts_dir) / f'pytest-{l.layer}.log'}\n" for l in suite.layers if l.artifacts_dir) +
                      "\n## Your job\nClassify every failing test as application_bug, test_defect or environment, with evidence. "
                      + ("Fix test defects now (only inside the test directory). Never weaken assertions to hide application bugs." if can_fix else "Do not modify tests in this call."))
            analysis = await self._agent("qa_engineer", prompt, task, S.QA_TESTING.value, read_dirs=read_dirs)
            if analysis.status == Status.ERROR or not analysis.output:
                return StageOutcome(error=True, reason=f"QA analysis error: {analysis.error}")
            classes = [f.classification for f in analysis.output.failure_analysis]
            app_bugs = [f for f in analysis.output.failure_analysis if f.classification == "application_bug"]
            env_issues = [f for f in analysis.output.failure_analysis if f.classification == "environment"]
            if can_fix and "test_defect" in classes and not app_bugs:
                fix_rounds += 1
                suite = await self._run_tests_with_server(task, "QA_TESTING")
                continue
            if env_issues and not app_bugs:
                return StageOutcome(block_reason="test environment problem: " + "; ".join(f"{e.test_id}: {e.explanation[:200]}" for e in env_issues[:3]))
            for bug in app_bugs or []:
                issue_id, created = self.state.add_issue(self.pid, task["id"], f"Test failure: {bug.test_id}", bug.severity.value, "qa", "qa_engineer",
                                                          bug.explanation + "\nEvidence: " + ", ".join(bug.evidence_artifacts), bug.file_path, bug.line_number)
                if created:
                    self.events.publish("BUG_CREATED", f"QA: {bug.test_id} - {bug.explanation[:200]}", status="OPEN", task_id=task["id"], agent="qa_engineer", metadata={"issue_id": issue_id})
            if not app_bugs:
                for failure in suite.failures()[:10]:
                    self.state.add_issue(self.pid, task["id"], f"Test failure: {failure['nodeid']}", "HIGH", "qa", "orchestrator", failure["message"][:2000])
                if suite.status == "ERROR" and not suite.failures():
                    self.state.add_issue(self.pid, task["id"], "Test run error", "HIGH", "qa", "orchestrator", self._suite_text(suite)[:3000])
            totals = suite.totals()
            self.notifier.notify("QA_FAILED", "QA Testing Failed", {"Task": task["title"], "Total Tests": totals["total"], "Passed": totals["passed"], "Failed": totals["failed"]},
                                 [b.explanation[:150] for b in app_bugs[:6]] or [f["nodeid"] for f in suite.failures()[:6]], footer="Bug Fixer has been assigned.")
            return self._repair(task, S.BUG_FIXING, f"QA failed: {totals['failed']} failing test(s)")
        if suite.status == "BLOCKED":
            reasons = "; ".join(f"{l.layer}: {l.summary}" for l in suite.layers if l.status == "BLOCKED" and l.required)
            return StageOutcome(block_reason=f"required testing layer cannot run in this environment: {reasons}")
        self.state.verify_pending_issues(task["id"], ["qa", "build"])
        totals = suite.totals()
        gui_blocked = [l for l in suite.layers if l.layer == "gui" and l.status == "BLOCKED"]
        self.notifier.notify("TESTS_PASSED", "Testing Passed", {"Task": task["title"], "Total": totals["total"], "Passed": totals["passed"]},
                             [f"{l.layer}: {l.status} ({l.summary})" for l in suite.layers], dedupe_key=f"qa-pass-{task['id']}-{task['retry_count']}")
        note = f" (GUI automation BLOCKED: {gui_blocked[0].summary})" if gui_blocked else ""
        return StageOutcome(S.SECURITY_TESTING, f"QA passed: {totals['passed']}/{totals['total']} tests{note}")

    async def _run_tests_with_server(self, task: dict[str, Any], stage: str) -> TestSuiteResult:
        assert self.deployer
        server = await self._start_qa_server(task)
        try:
            if server is not None and not server.ok:
                from test_manager import LayerResult

                failure = LayerResult("e2e", "playwright", "FAIL", summary=f"application failed to start for browser tests: {server.error}",
                                      failures=[{"layer": "e2e", "nodeid": "<application startup>", "status": "ERROR", "message": (server.detail.get("log_tail") or server.error or "")[:2000]}])
                suite = await self._run_tests(task, stage, None)
                suite.layers = [l for l in suite.layers if l.layer != "e2e"] + [failure]
                return suite
            return await self._run_tests(task, stage, server.url if server else None)
        finally:
            if server is not None and server.deployment_id:
                self.deployer.stop(server.deployment_id)

    # ======================================================= stage: SECURITY
    async def _stage_security(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.scanner and self.workspace
        dependency_scan = self.scanner.needs_dependency_scan(self.workspace)
        async with self._serial:
            results = await asyncio.to_thread(self.scanner.run_all, self.workspace, task["id"], dependency_scan=dependency_scan)
        open_findings = self.state.open_security_findings(self.pid)[:80]
        scan_text = "\n".join(f"- {r.tool}: {r.status} ({len(r.findings)} finding(s))" + (f" ERROR: {r.error}" if r.error else "") + (f" notes: {'; '.join(r.notes[:3])}" if r.notes else "") for r in results)
        finding_text = "\n".join(f"- finding_id {f['id']} | {f['tool_name']} | {f['severity']} | {f.get('file_path')}:{f.get('line_number')} | {f.get('rule_id')} | {f['description'][:300]}" for f in open_findings) or "- none"
        diff = self.git.diff(self.workspace, task.get("base_commit"), max_chars=60_000)
        read_dirs = sorted({Path(r.report_path).parent for r in results if r.report_path})
        prompt = (f"{self._task_context(task)}\n\n## Scanner runs (executed by the orchestrator)\n{scan_text}\n\n## Open findings\n{finding_text}\n\n"
                  f"## Change under review\n```diff\n{diff}\n```\n\nAssess every finding with evidence and perform the manual security checks on the change.")
        result = await self._agent("security_auditor", prompt, task, S.SECURITY_TESTING.value, read_dirs=read_dirs)
        if result.status == Status.ERROR or not result.output:
            return StageOutcome(error=True, reason=f"security auditor error: {result.error}")
        out = result.output
        valid_ids = {f["id"] for f in open_findings}
        for assessment in out.assessments:
            if assessment.finding_id not in valid_ids or not assessment.evidence.strip():
                continue
            status = {"VERIFIED": "VERIFIED", "LIKELY_FALSE_POSITIVE": "FALSE_POSITIVE_SUSPECTED", "NEEDS_HUMAN_REVIEW": "NEEDS_REVIEW"}[assessment.verdict]
            self.state.update_security_finding(assessment.finding_id, status=status, auditor_assessment=assessment.model_dump())
        for finding in out.findings:
            if not finding.evidence.strip():
                continue  # the auditor must provide evidence for every finding
            fp = fingerprint("security_auditor", finding.title, finding.file_path or "")
            scan_id = self.state.add_security_scan(self.pid, task["id"], tool_name="security_auditor", status="COMPLETED", findings_count=1)
            self.state.upsert_security_finding(self.pid, task["id"], scan_id, {
                "tool_name": "security_auditor", "rule_id": finding.category or "manual-review", "severity": finding.severity.value,
                "confidence": "MEDIUM", "file_path": finding.file_path, "line_number": finding.line_number,
                "description": f"{finding.title}: {finding.explanation} Evidence: {finding.evidence}", "fingerprint": fp,
            }, baseline=False)
        decision = self.scanner.evaluate_policy(results, set(self._changed_files(task)))
        report = self._write_report("security", f"task-{task['id']}-security-{self.state.count_stage_runs(task['id'], S.SECURITY_TESTING.value)}.json",
                                    {"scans": [r.__dict__ for r in results], "auditor": out.model_dump(), "decision": decision.__dict__})
        for f in decision.blocking[:10]:
            self.events.publish("SECURITY_FINDING", f"[{f['severity']}] {f['tool_name']} {f.get('file_path')}:{f.get('line_number')} {f['description'][:200]}", status="BLOCKING", task_id=task["id"], metadata={"finding_id": f["id"]})
        if decision.status == "PASS":
            self.state.verify_pending_issues(task["id"], ["security"])
            return StageOutcome(S.LOCAL_STAGING_DEPLOYMENT, decision.summary() + (f"; warnings: {len(decision.warnings)}" if decision.warnings else ""))
        if decision.status == "BLOCKED":
            for f in decision.needs_human_review:
                self.state.request_approval(self.pid, "SECURITY_EXCEPTION", f"task:{task['id']}", {"fingerprints": [x["fingerprint"] for x in decision.needs_human_review], "findings": [x["description"][:300] for x in decision.needs_human_review]})
                break
            self.notifier.notify("SECURITY_FINDINGS", "Security Findings - review required", {"Task": task["title"], "Status": "Sent for security review"},
                                 [f"{f['tool_name']} {f['severity']} {f.get('file_path')}: {f['description'][:120]}" for f in decision.needs_human_review[:6]] + decision.scanner_problems[:4])
            return StageOutcome(block_reason=f"security review required: {decision.summary()} (report {report.name})")
        for f in decision.blocking:
            self.state.add_issue(self.pid, task["id"], f"Security: {f.get('rule_id')} in {f.get('file_path')}", f["severity"], "security", f["tool_name"],
                                 f["description"], f.get("file_path"), f.get("line_number"), "Fix the vulnerable code; do not suppress the finding.")
        top = decision.blocking[0]
        self.notifier.notify("SECURITY_FINDINGS", "Security Findings", {"Task": task["title"], "Tool": top["tool_name"], "Severity": top["severity"], "File": top.get("file_path"), "Issue": top["description"][:200], "Blocking findings": len(decision.blocking)},
                             footer="Status: sent to the Bug Fixer.")
        return self._repair(task, S.BUG_FIXING, f"security policy failed: {decision.summary()}")

    # ===================================================== stage: BUG FIXING
    async def _stage_bug_fixing(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        issues = self.state.open_issues(task["id"])
        if not issues:
            return StageOutcome(S.CODE_REVIEW if self.config.workflow.review_after_fix else S.QA_TESTING, "no open issues to fix; re-verifying")
        issue_files = sorted({i["file_path"] for i in issues if i.get("file_path") and i["file_path"] not in ("environment",) and "," not in i["file_path"]})
        latest_failures = [f for r in self.state.list_test_runs(self.pid, task["id"], 10) for f in self.state.test_cases(r["id"]) if f["status"] in ("FAIL", "ERROR")][:10]
        issue_text = "\n".join(f"- [issue_id {i['id']}] ({i['category']}, {i['severity']}, from {i['source_agent']}) {i['title']} | {i.get('file_path') or '-'}:{i.get('line_number') or ''}\n  {i['description'][:1200]}\n  Recommendation: {(i.get('recommendation') or '')[:400]}" for i in issues)
        failure_text = "\n".join(f"- {c['nodeid']}: {c['message'][:800]}" for c in latest_failures) or "- none"
        prompt = (f"{self._task_context(task)}\n\n## Open issues to fix\n{issue_text}\n\n## Latest failing tests\n{failure_text}\n\n"
                  "Fix each issue with the smallest correct change and add a regression test. Report every issue_id you fixed; list the rest as unresolved.")
        result = await self._agent("bug_fixer", prompt, task, S.BUG_FIXING.value, replacements={"task_scope": self._task_scope(task), "issue_files": issue_files or ["**"]})
        if result.status == Status.ERROR or not result.output:
            return StageOutcome(error=True, reason=f"bug fixer error: {result.error}")
        out = result.output
        known = {i["id"] for i in issues}
        fixed_ids = [f.issue_id for f in out.fixes if f.issue_id in known and f.issue_id not in out.unresolved_issue_ids]
        for fix in out.fixes:
            if fix.issue_id in fixed_ids:
                self.state.set_issue_status([fix.issue_id], "FIXED_PENDING_VERIFICATION", f"{fix.root_cause} -> {fix.fix_description}", increment_attempts=True)
        if fixed_ids:
            self.events.publish("BUG_FIXED", f"#{task['id']}: {len(fixed_ids)} issue(s) fixed (pending independent verification)", status="PENDING_VERIFICATION", task_id=task["id"], agent="bug_fixer", metadata={"issue_ids": fixed_ids})
            self.notifier.notify("BUG_FIXED", "Bug fix applied", {"Task": task["title"], "Fixed (unverified)": len(fixed_ids), "Unresolved": len(out.unresolved_issue_ids)},
                                 [f.fix_description[:150] for f in out.fixes[:6]], footer="Re-running review, QA and security checks.")
        compile_error = self._compile_check(self._changed_files(task))
        if compile_error:
            self.state.add_issue(self.pid, task["id"], "Python files do not compile after fix", "HIGH", "build", "orchestrator", compile_error)
        target = S.CODE_REVIEW if self.config.workflow.review_after_fix and self.config.project.require_independent_review else S.QA_TESTING
        return StageOutcome(target, f"bug fixer addressed {len(fixed_ids)}/{len(issues)} issue(s); verifying independently")

    # ======================================================= stage: STAGING
    async def _stage_staging(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.deployer and self.workspace and self.profile
        method = self.deployer.method(self.workspace)
        if method == "none":
            self._save_design(task["id"], staging={"status": "NOT_APPLICABLE", "reason": "no runnable web or desktop application detected"})
            return StageOutcome(S.PRODUCT_VALIDATION, "staging not applicable (library project)")
        if method == "desktop":
            result = await self.deployer.deploy_desktop(self.workspace, task["id"])
        else:
            result = await self.deployer.deploy_flask(self.workspace, task["id"], purpose="staging")
        fields = {"Task": task["title"], "Method": result.method, "Status": "PASS" if result.ok else "FAIL", "URL": result.url or "-"}
        footer = None
        if result.ok and result.url:
            if self.config.project.flask.lan_access:
                fields["Remote URL"] = result.detail.get("remote_url") or "not available (no network connection found)"
            else:
                fields["Remote URL"] = "off (enable network access in the project settings)"
            footer = "The staging app runs only while this task is tested and validated, then it is stopped."
        self.notifier.notify("STAGING_DEPLOYMENT", "Staging Deployment", fields,
                             [result.error] if result.error else None, footer=footer, dedupe_key=f"staging-{task['id']}-{result.deployment_id}")
        if not result.ok:
            return await self._handle_staging_failure(task, result)
        self._save_design(task["id"], staging={"deployment_id": result.deployment_id, "url": result.url, "method": result.method, "detail": {k: v for k, v in result.detail.items() if k != "log_tail"}})
        if method == "desktop":
            suite = await self._run_tests(task, S.LOCAL_STAGING_DEPLOYMENT.value, None)
        else:
            suite = await self._run_tests(task, S.LOCAL_STAGING_DEPLOYMENT.value, result.url)
        if suite.status in ("FAIL", "ERROR"):
            if result.method != "desktop" and result.deployment_id:
                self.deployer.stop(result.deployment_id)
            for failure in suite.failures()[:10]:
                self.state.add_issue(self.pid, task["id"], f"Staging test failure: {failure['nodeid']}", "HIGH", "staging", "orchestrator", failure["message"][:2000])
            return self._repair(task, S.BUG_FIXING, f"tests against staging failed ({suite.totals()['failed']} failing)")
        if suite.status == "BLOCKED":
            if result.method != "desktop" and result.deployment_id:
                self.deployer.stop(result.deployment_id)
            return StageOutcome(block_reason="required staging test layer cannot run: " + "; ".join(l.summary for l in suite.layers if l.status == "BLOCKED" and l.required))
        self.state.kv_set(f"staging_deployment:{task['id']}", str(result.deployment_id or ""))
        return StageOutcome(S.PRODUCT_VALIDATION, f"staging healthy ({result.method}{' at ' + result.url if result.url else ''}); staging tests passed")

    async def _handle_staging_failure(self, task: dict[str, Any], result: DeploymentResult) -> StageOutcome:
        retry_key = f"devops_retry:{task['id']}:{task['retry_count']}"
        prompt = (f"{self._task_context(task)}\n\n## Failed staging deployment\nMethod: {result.method}\nError: {result.error}\n"
                  f"Command: {(result.detail or {}).get('command', '')}\nEnvironment variable names: {(result.detail or {}).get('env_vars', [])}\n\n"
                  f"## Startup log (tail)\n```\n{redact((result.detail or {}).get('log_tail', ''))[-6000:]}\n```\n\nDiagnose the failure.")
        analysis = await self._agent("devops_engineer", prompt, task, S.LOCAL_STAGING_DEPLOYMENT.value, read_dirs=[Path(result.log_path).parent] if result.log_path else [])
        if analysis.status == Status.ERROR or not analysis.output:
            return StageOutcome(error=True, reason=f"devops analysis error: {analysis.error}")
        out = analysis.output
        if out.config_changes_made and not out.requires_code_fix and self.state.kv_get(retry_key) != "1":
            self.state.kv_set(retry_key, "1")
            return StageOutcome(S.LOCAL_STAGING_DEPLOYMENT, f"devops changed staging configuration ({', '.join(out.config_changes_made)[:200]}); redeploying")
        if out.requires_code_fix or out.root_cause_category in ("application_code", "dependency"):
            self._record_findings_as_issues(task, out.findings, "staging", "devops_engineer")
            if not out.findings:
                self.state.add_issue(self.pid, task["id"], "Application fails to start in staging", "HIGH", "staging", "devops_engineer", out.diagnosis)
            return self._repair(task, S.BUG_FIXING, f"staging deployment failed: {out.diagnosis[:300]}")
        return StageOutcome(block_reason=f"staging environment problem ({out.root_cause_category}): {out.diagnosis[:500]}")

    # ==================================================== stage: VALIDATION
    async def _stage_validation(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.workspace and self.test_manager and self.deployer and self.profile
        criteria = self._criteria(task)
        staging = self._design(task).get("staging") or {}
        deployment_id = staging.get("deployment_id")
        url = staging.get("url")
        if url:
            row = self.state.query_one("SELECT status FROM deployments WHERE id = ?", (deployment_id,)) if deployment_id else None
            if not row or row["status"] != "HEALTHY":
                # e.g. resumed after a restart: staging is gone, redeploy first
                result = await self.deployer.deploy_flask(self.workspace, task["id"], purpose="staging")
                if not result.ok:
                    return self._repair(task, S.BUG_FIXING, f"staging redeploy for validation failed: {result.error}")
                deployment_id, url = result.deployment_id, result.url
                self._save_design(task["id"], staging={**staging, "deployment_id": deployment_id, "url": url})
        try:
            env_text = (f"Flask application running in local staging at {url} (health endpoint {self.config.project.flask.health_path}). "
                        "APP_BASE_URL is set to this URL when the orchestrator runs your tests; Playwright tests receive it as --base-url. The staging database is isolated.") if url else (
                        f"Desktop application (entry `{self.config.project.pyside.entry or self.profile.pyside_entry}`); tests run with QT_QPA_PLATFORM=offscreen." if "pyside" in self.profile.application_types else "Library project without a running application.")
            prompt = (f"{self._task_context(task)}\n\n## Staging environment\n{env_text}\n\n## Your job\nIndependently validate the implementation against each acceptance criterion. "
                      f"Write acceptance tests in `{self.config.project.test_dir}/acceptance/test_task_{task['id']}_*.py`, run them, and report a verdict per criterion "
                      "using the exact criterion text below, with the pytest node ids that prove it.\n\nCriteria:\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria)))
            result = await self._agent("product_validator", prompt, task, S.PRODUCT_VALIDATION.value)
            if result.status == Status.ERROR or not result.output:
                return StageOutcome(error=True, reason=f"validator error: {result.error}")
            out = result.output
            verdict, problems, evidence_ids, criteria_map = self._verify_validation_claims(criteria, out)
            acceptance_run = None
            if verdict:
                layers = self.test_manager.acceptance_layers(self.workspace, url, sorted({e.split("::")[0] for e in evidence_ids}))
                async with self._serial:
                    suite = await asyncio.to_thread(self.test_manager.run_suite, layers, self.workspace, task["id"], S.PRODUCT_VALIDATION.value)
                acceptance_run = suite.layers[0]
                cases = self.state.test_cases(acceptance_run.test_run_id) if acceptance_run.test_run_id else []
                passed_ids = [c["nodeid"] for c in cases if c["status"] == "PASS"]
                for evidence in evidence_ids:
                    if not any(p == evidence or p.startswith(evidence + "[") or p.endswith("/" + evidence) for p in passed_ids):
                        problems.append(f"evidence test {evidence} did not pass in the orchestrator's own run")
                if acceptance_run.status != "PASS":
                    problems.append(f"acceptance run {acceptance_run.status}: {acceptance_run.summary}")
                regression = await self._run_tests(task, S.PRODUCT_VALIDATION.value, url)
                if regression.status in ("FAIL", "ERROR"):
                    problems.append(f"regression suite {regression.status}: " + "; ".join(f["nodeid"] for f in regression.failures()[:5]))
                verdict = not problems
            self.events.publish("VALIDATION_COMPLETED", f"#{task['id']} validation {'PASS' if verdict else 'FAIL'}" + (f": {'; '.join(problems)[:300]}" if problems else ""), status="PASS" if verdict else "FAIL", task_id=task["id"], agent="product_validator")
            self.notifier.notify("VALIDATION_RESULT", "Product Validation " + ("Passed" if verdict else "Failed"), {"Task": task["title"], "Criteria": f"{sum(1 for c in out.criteria_results if c.verdict == 'PASS')}/{len(criteria)} PASS"}, problems[:6] or None, dedupe_key=f"validation-{task['id']}-{task['retry_count']}-{verdict}")
            self._write_report("releases", f"task-{task['id']}-validation.json", {"validator": out.model_dump(), "verdict": verdict, "problems": problems, "evidence": evidence_ids})
            if verdict:
                self._save_design(task["id"], validation_evidence={
                    "task_id": task["id"], "criteria": criteria_map, "acceptance_test_run_id": acceptance_run.test_run_id if acceptance_run else None,
                    "validator_agent_run_id": result.agent_run_id, "staging_deployment_id": deployment_id, "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                self.state.verify_pending_issues(task["id"], ["validation", "staging"])
                return StageOutcome(S.GIT_CHECKPOINT, "independent validation passed and was verified by the orchestrator")
            for problem in problems:
                self.state.add_issue(self.pid, task["id"], f"Validation: {problem[:180]}", "HIGH", "validation", "product_validator", problem)
            for item in out.criteria_results:
                if item.verdict != "PASS":
                    self.state.add_issue(self.pid, task["id"], f"Acceptance criterion failed: {item.criterion[:180]}", "HIGH", "validation", "product_validator", item.notes)
            target = S.DEVELOPMENT if out.incomplete_requirements else S.BUG_FIXING
            return self._repair(task, target, "product validation failed: " + "; ".join(problems or [out.summary])[:400])
        finally:
            if deployment_id:
                self.deployer.stop(deployment_id)

    def _verify_validation_claims(self, criteria: list[str], out) -> tuple[bool, list[str], list[str], list[dict[str, Any]]]:
        problems: list[str] = []
        evidence: list[str] = []
        mapping: list[dict[str, Any]] = []
        if out.status != Status.PASS or not out.recommend_completion:
            problems.append(f"validator did not recommend completion: {out.summary[:300]}")
        norm = lambda s: " ".join(s.lower().split()).rstrip(".")
        results = list(out.criteria_results)
        for index, criterion in enumerate(criteria):
            match = next((r for r in results if norm(r.criterion) == norm(criterion)), None)
            if match is None:
                match = next((r for r in results if norm(criterion) in norm(r.criterion) or norm(r.criterion) in norm(criterion)), None)
            if match is None and len(results) == len(criteria):
                match = results[index]
            if match is None:
                problems.append(f"no validation result for criterion: {criterion[:120]}")
                continue
            ids = [e.replace("\\", "/").removeprefix("./") for e in match.evidence_test_ids if e.strip()]
            if match.verdict != "PASS":
                problems.append(f"criterion {match.verdict}: {criterion[:120]}")
            elif not ids:
                problems.append(f"criterion has no executable evidence: {criterion[:120]}")
            evidence.extend(ids)
            mapping.append({"criterion": criterion, "verdict": match.verdict, "tests": ids})
        if not criteria:
            problems.append("task has no acceptance criteria; it cannot be validated")
        return not problems, problems, sorted(set(evidence)), mapping

    # ===================================================== stage: CHECKPOINT
    async def _stage_checkpoint(self, task: dict[str, Any], interrupted: bool) -> StageOutcome:
        assert self.workspace
        branch = self.git.current_branch(self.workspace) or self.config.project.development_branch
        protected = {self.config.project.default_branch, "main", "master"}
        sha = self.git.find_task_commit(self.workspace, task["id"])  # a crash after commit but before recording
        description = f"task #{task['id']}: {task['title']}"
        if sha:
            note = "checkpoint commit already existed (recovered after interruption)"
        else:
            prefix = KIND_COMMIT_PREFIX.get(task["kind"], "feat")
            message = f"{prefix}: {task['title'][:60].rstrip('.').lower()}"
            async with self._serial:
                sha = await asyncio.to_thread(self.git.commit_changes, self.workspace, message, task["id"], protected, self._commit_body(task))
            note = "committed"
            if sha is None:
                sha = self.git.head_commit(self.workspace)
                note = "no file changes to commit; checkpoint references the current HEAD"
        if not sha:
            if self.config.project.require_git_checkpoint:
                return StageOutcome(block_reason="git checkpoint required but no commit could be created")
            sha = "none"
        self.state.add_checkpoint(self.pid, task["id"], sha, branch, description)
        design = self._design(task)
        evidence = dict(design.get("validation_evidence") or {})
        evidence["commit"] = sha
        self._save_design(task["id"], validation_evidence=evidence)
        self.events.publish("CHECKPOINT_CREATED", f"#{task['id']} {sha[:10]} on {branch} ({note})", status="OK", task_id=task["id"], metadata={"commit": sha, "branch": branch})
        await self._push(f"task #{task['id']} checkpoint", task["id"])
        return StageOutcome(S.COMPLETED, f"checkpoint {sha[:10]} ({note})")

    def _commit_body(self, task: dict[str, Any]) -> list[str]:
        """Commit message paragraphs: what was built, what was fixed on the way, and what was validated."""
        design = self._design(task)
        body: list[str] = []
        if task.get("description"):
            body.append(task["description"][:600])
        if design.get("developer_summary"):
            body.append("Implementation: " + str(design["developer_summary"])[:600])
        fixed = [i for i in self.state.issues_for_task(task["id"]) if i["status"] == "VERIFIED"]
        if fixed:
            body.append("Fixed during review/testing: " + "; ".join(f"[{i['category']}] {i['title']}" for i in fixed[:12])[:1200])
        criteria = self._criteria(task)
        if criteria:
            body.append("Validated acceptance criteria: " + "; ".join(criteria)[:1200])
        return body
