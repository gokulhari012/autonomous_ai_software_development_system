"""Local staging deployment (never production).

* Flask: started as a local process (``python -m flask --app ... run``) or with
  Docker Compose when a ``docker-compose.staging.yml`` exists and Docker is
  available. Each deployment gets its own staging SQLite database and freshly
  generated staging-only credentials. A deployment is PASS only when the health
  endpoint answers 200 (and reports a working database when it says so).
* PySide: the application is launched locally (offscreen Qt platform unless real
  GUI automation is authorised); it must start and stay up without crashing.
  When a packaging command is configured, the executable is built and launched too.

Production deployment is not implemented on purpose: the configuration refuses
``production_deployment: true``. Staging listens on localhost only, unless the
project opts in to ``flask.lan_access`` so devices on the local network can open it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as pysecrets
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psutil

from config import REPORTS_DIR, AppConfig
from events import EventBus
from net_utils import lan_ip
from project_inspector import ProjectProfile, gui_session_available
from safe_exec import SafeExecutor, find_executable, kill_process_tree
from state_manager import StateManager

log = logging.getLogger("deployment")


@dataclass
class DeploymentResult:
    status: str  # HEALTHY | FAILED | NOT_APPLICABLE
    method: str
    deployment_id: int | None = None
    url: str | None = None
    log_path: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "HEALTHY"


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def tail(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


class DeploymentManager:
    def __init__(self, config: AppConfig, state: StateManager, events: EventBus, executor: SafeExecutor, profile: ProjectProfile):
        if config.project.production_deployment:  # defence in depth; config validation already refuses this
            raise RuntimeError("production deployment is disabled")
        self.config = config
        self.state = state
        self.events = events
        self.executor = executor
        self.profile = profile
        self.python = config.project.python_executable or sys.executable
        self._processes: dict[int, Any] = {}

    # ------------------------------------------------------------- planning
    def method(self, workspace: Path) -> str:
        configured = self.config.project.deployment_method
        types = set(self.profile.application_types)
        if "flask" in types:
            compose = workspace / "docker-compose.staging.yml"
            if configured in ("auto", "docker-compose") and compose.exists() and find_executable("docker"):
                return "docker-compose"
            return "process"
        if "pyside" in types:
            return "desktop"
        return "none"

    def _staging_dir(self, label: str) -> Path:
        path = REPORTS_DIR / "staging" / self.config.project_id / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ----------------------------------------------------------------- flask
    async def deploy_flask(self, workspace: Path, task_id: int | None, purpose: str = "staging") -> DeploymentResult:
        flask_cfg = self.config.project.flask
        method = self.method(workspace)
        port = flask_cfg.staging_port if purpose == "staging" else flask_cfg.qa_port
        host = flask_cfg.staging_host
        if host not in ("127.0.0.1", "localhost"):
            return DeploymentResult("FAILED", method, error="staging must bind to localhost")
        url = f"http://{host}:{port}"
        # Only the staging server is ever shared on the network, never the temporary QA server.
        shared = flask_cfg.lan_access and purpose == "staging"
        bind_host = "0.0.0.0" if shared else host
        address = lan_ip() if shared else None
        remote_url = f"http://{address}:{port}" if address else None
        staging_dir = self._staging_dir(purpose)
        log_path = staging_dir / "startup.log"
        self.events.publish("DEPLOYMENT_STARTED", f"{purpose} deployment starting ({method}) at {url}", status="IN_PROGRESS", task_id=task_id, metadata={"method": method, "purpose": purpose})

        if port_in_use(host, port):
            self.stop_stale(port_hint=port)
            await asyncio.sleep(1)
            if port_in_use(host, port):
                return self._failed(task_id, method, url, log_path, f"port {port} is already in use by another process; refusing to stop a process the controller did not start")

        staging_secret = pysecrets.token_urlsafe(32)  # staging-only credential, never logged
        env = {
            "APP_ENV": "staging",
            "FLASK_DEBUG": "0",
            "SECRET_KEY": staging_secret,
            flask_cfg.database_env_var: "sqlite:///" + (staging_dir / "staging.db").as_posix(),
            "PYTHONPATH": str(workspace),
        }
        if method == "docker-compose":
            result = await self._deploy_compose(workspace, task_id, url, staging_dir, log_path, env)
            if result.ok and remote_url:  # the compose file decides which interfaces the port is published on
                result.detail["remote_url"] = remote_url
            return result

        app_spec = flask_cfg.app or self.profile.flask_app
        if not app_spec and not flask_cfg.start_command:
            return self._failed(task_id, method, url, log_path, "could not determine the Flask application (set flask.app in project.json)")
        if flask_cfg.start_command:
            argv = [a.format(python=self.python, port=port, host=bind_host) for a in flask_cfg.start_command]
        else:
            argv = [self.python, "-m", "flask", "--app", app_spec, "run", "--host", bind_host, "--port", str(port), "--no-reload", "--no-debugger"]
        try:
            proc = self.executor.spawn(argv, workspace, log_path, env)
        except Exception as exc:
            return self._failed(task_id, method, url, log_path, f"failed to start: {exc}")
        self._processes[proc.pid] = proc
        create_time = psutil.Process(proc.pid).create_time()
        deployment_id = self.state.add_deployment(
            self.config.project_id, task_id, environment=purpose, method=method, status="STARTING", url=url, pid=proc.pid,
            pid_create_time=create_time, log_path=str(log_path), detail={"command": " ".join(argv[:6]), "env_vars": sorted(env)},
        )
        health = await self._wait_healthy(url + flask_cfg.health_path, proc, flask_cfg.startup_timeout_seconds)
        detail = {"health": health, "env_vars": sorted(env), "staging_db": str(staging_dir / "staging.db")}
        if health["ok"]:
            if remote_url:
                detail["remote_url"] = remote_url
            self.state.update_deployment(deployment_id, status="HEALTHY", health_status=json.dumps(health)[:1000], detail=detail)
            shared_text = f" (on the local network: {remote_url})" if remote_url else ""
            self.events.publish("DEPLOYMENT_COMPLETED", f"{purpose} deployment healthy at {url}{shared_text}", status="PASS", task_id=task_id, metadata={"deployment_id": deployment_id, **health})
            return DeploymentResult("HEALTHY", method, deployment_id, url, str(log_path), detail)
        self.stop(deployment_id)
        self.state.update_deployment(deployment_id, status="FAILED", health_status=json.dumps(health)[:1000], detail={**detail, "log_tail": tail(log_path, 40)})
        self.events.publish("DEPLOYMENT_FAILED", f"{purpose} deployment failed: {health.get('error')}", status="FAIL", task_id=task_id, metadata={"deployment_id": deployment_id})
        return DeploymentResult("FAILED", method, deployment_id, url, str(log_path), {**detail, "log_tail": tail(log_path, 80)}, health.get("error"))

    async def _wait_healthy(self, health_url: str, proc: Any, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error = "no response"
        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                if proc is not None and proc.poll() is not None:
                    return {"ok": False, "error": f"process exited with code {proc.returncode} during startup"}
                try:
                    response = await client.get(health_url)
                    body: Any
                    try:
                        body = response.json()
                    except ValueError:
                        body = response.text[:200]
                    if response.status_code == 200:
                        if isinstance(body, dict) and str(body.get("database", "ok")).lower() not in ("ok", "up", "true", "healthy"):
                            return {"ok": False, "status_code": 200, "body": body, "error": "health endpoint reports database problem"}
                        return {"ok": True, "status_code": 200, "body": body}
                    last_error = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = type(exc).__name__
                await asyncio.sleep(0.5)
        return {"ok": False, "error": f"health check did not succeed within {timeout}s (last: {last_error})"}

    async def _deploy_compose(self, workspace: Path, task_id: int | None, url: str, staging_dir: Path, log_path: Path, env: dict[str, str]) -> DeploymentResult:
        docker = find_executable("docker") or "docker"
        project_name = f"autodev-{self.config.project_id}"
        argv = [docker, "compose", "-f", "docker-compose.staging.yml", "-p", project_name, "up", "-d", "--build"]
        run = self.executor.run(argv, workspace, timeout=900, env_extra=env)
        log_path.write_text(run.stdout + "\n" + run.stderr, encoding="utf-8")
        deployment_id = self.state.add_deployment(self.config.project_id, task_id, method="docker-compose", status="STARTING", url=url, log_path=str(log_path), detail={"compose_project": project_name})
        if not run.ok:
            self.state.update_deployment(deployment_id, status="FAILED")
            return DeploymentResult("FAILED", "docker-compose", deployment_id, url, str(log_path), {"log_tail": tail(log_path)}, f"docker compose up failed ({run.exit_code})")
        health = await self._wait_healthy(url + self.config.project.flask.health_path, None, self.config.project.flask.startup_timeout_seconds + 60)
        ps = self.executor.run([docker, "compose", "-p", project_name, "ps", "--format", "json"], workspace, timeout=60)
        detail = {"health": health, "containers": ps.stdout[:4000]}
        if health["ok"]:
            self.state.update_deployment(deployment_id, status="HEALTHY", health_status=json.dumps(health)[:1000], detail=detail)
            self.events.publish("DEPLOYMENT_COMPLETED", f"staging (docker compose) healthy at {url}", status="PASS", task_id=task_id, metadata={"deployment_id": deployment_id})
            return DeploymentResult("HEALTHY", "docker-compose", deployment_id, url, str(log_path), detail)
        self.stop(deployment_id)
        return DeploymentResult("FAILED", "docker-compose", deployment_id, url, str(log_path), detail, health.get("error"))

    def _failed(self, task_id: int | None, method: str, url: str, log_path: Path, error: str) -> DeploymentResult:
        deployment_id = self.state.add_deployment(self.config.project_id, task_id, method=method, status="FAILED", url=url, log_path=str(log_path), detail={"error": error})
        self.events.publish("DEPLOYMENT_FAILED", error, status="FAIL", task_id=task_id, metadata={"deployment_id": deployment_id})
        return DeploymentResult("FAILED", method, deployment_id, url, str(log_path), {"error": error}, error)

    # --------------------------------------------------------------- desktop
    async def deploy_desktop(self, workspace: Path, task_id: int | None) -> DeploymentResult:
        pyside = self.config.project.pyside
        entry = pyside.entry or self.profile.pyside_entry
        staging_dir = self._staging_dir("desktop")
        log_path = staging_dir / "app.log"
        if not entry:
            return self._failed(task_id, "desktop", "", log_path, "could not determine the PySide entry point (set pyside.entry in project.json)")
        gui_ok, gui_reason = gui_session_available()
        real_gui = gui_ok and self.config.testing.pyside.allow_real_input_automation
        env = {"APP_ENV": "staging", "APP_DATA_DIR": str(staging_dir / "data"), "PYTHONPATH": str(workspace)}
        if not real_gui:
            env["QT_QPA_PLATFORM"] = "offscreen"
        argv = [self.python, entry] if entry.endswith(".py") else [self.python, "-m", entry]
        self.events.publish("DEPLOYMENT_STARTED", f"launching desktop application {entry}", status="IN_PROGRESS", task_id=task_id, metadata={"method": "desktop", "qt_platform": env.get("QT_QPA_PLATFORM", "native")})
        result = await self._launch_and_watch(argv, workspace, log_path, env, task_id, pyside.startup_seconds, "desktop")
        result.detail["display"] = "native" if real_gui else f"offscreen ({gui_reason})"
        if result.ok and self.config.testing.pyside.packaged_application_testing and pyside.package_command:
            packaged = await self.deploy_packaged(workspace, task_id)
            result.detail["packaged"] = {"status": packaged.status, "error": packaged.error}
            if not packaged.ok:
                result.status, result.error = "FAILED", f"packaged application failed: {packaged.error}"
        elif self.config.testing.pyside.packaged_application_testing and not pyside.package_command:
            result.detail["packaged"] = {"status": "NOT_APPLICABLE", "reason": "no packaging command configured"}
        return result

    async def deploy_packaged(self, workspace: Path, task_id: int | None) -> DeploymentResult:
        pyside = self.config.project.pyside
        staging_dir = self._staging_dir("packaged")
        build_log = staging_dir / "build.log"
        self.executor.policy.allow_pyinstaller = True
        argv = [a.format(python=self.python) for a in (pyside.package_command or [])]
        build = self.executor.run(argv, workspace, timeout=1800)
        build_log.write_text(build.stdout + "\n" + build.stderr, encoding="utf-8")
        if not build.ok:
            return self._failed(task_id, "packaged", "", build_log, f"packaging failed (exit {build.exit_code})")
        exe = (workspace / (pyside.executable_path or "")).resolve()
        if not pyside.executable_path or not exe.exists():
            return self._failed(task_id, "packaged", "", build_log, f"built executable not found at {pyside.executable_path}")
        self.executor.policy.app_executables.append(str(exe))
        env = {"APP_ENV": "staging", "APP_DATA_DIR": str(staging_dir / "data")}
        if not (gui_session_available()[0] and self.config.testing.pyside.allow_real_input_automation):
            env["QT_QPA_PLATFORM"] = "offscreen"
        return await self._launch_and_watch([str(exe)], workspace, staging_dir / "app.log", env, task_id, pyside.startup_seconds, "packaged")

    async def _launch_and_watch(self, argv: list[str], workspace: Path, log_path: Path, env: dict[str, str], task_id: int | None, seconds: int, method: str) -> DeploymentResult:
        try:
            proc = self.executor.spawn(argv, workspace, log_path, env)
        except Exception as exc:
            return self._failed(task_id, method, "", log_path, f"failed to launch: {exc}")
        deployment_id = self.state.add_deployment(self.config.project_id, task_id, method=method, status="STARTING", pid=proc.pid, pid_create_time=psutil.Process(proc.pid).create_time(), log_path=str(log_path), detail={"command": " ".join(argv[:4])})
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and proc.poll() is None:
            await asyncio.sleep(0.25)
        exit_code = proc.poll()
        log_text = tail(log_path, 60)
        crashed = exit_code not in (None, 0) or "Traceback (most recent call last)" in log_text
        if crashed:
            kill_process_tree(proc.pid)
            self.state.update_deployment(deployment_id, status="FAILED", stopped_at=time.strftime("%Y-%m-%dT%H:%M:%S"), detail={"exit_code": exit_code, "log_tail": log_text})
            self.events.publish("DEPLOYMENT_FAILED", f"{method} application crashed during startup (exit {exit_code})", status="FAIL", task_id=task_id, metadata={"deployment_id": deployment_id})
            return DeploymentResult("FAILED", method, deployment_id, None, str(log_path), {"exit_code": exit_code, "log_tail": log_text}, f"application crashed during startup (exit code {exit_code})")
        still_running = exit_code is None
        if still_running:
            kill_process_tree(proc.pid)  # shutdown after a successful startup window
        detail = {"ran_for_seconds": seconds, "exited_on_its_own": not still_running, "exit_code": exit_code, "log_tail": log_text[-2000:]}
        self.state.update_deployment(deployment_id, status="STOPPED", health_status="started without crash", detail=detail, stopped_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.events.publish("DEPLOYMENT_COMPLETED", f"{method} application started successfully and was shut down", status="PASS", task_id=task_id, metadata={"deployment_id": deployment_id})
        return DeploymentResult("HEALTHY", method, deployment_id, None, str(log_path), detail)

    # ----------------------------------------------------------------- stop
    def stop(self, deployment_id: int) -> None:
        row = self.state.query_one("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
        if not row:
            return
        if row["method"] == "docker-compose":
            docker = find_executable("docker") or "docker"
            project_name = (row.get("detail") or {}).get("compose_project", f"autodev-{self.config.project_id}")
            try:
                self.executor.run([docker, "compose", "-p", project_name, "down"], Path(self.config.worktree_path()), timeout=300)
            except Exception:
                log.exception("docker compose down failed")
        elif row.get("pid"):
            if self._owns_process(row):
                kill_process_tree(row["pid"])
        self.state.update_deployment(deployment_id, status="STOPPED" if row["status"] != "FAILED" else "FAILED", stopped_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.events.publish("DEPLOYMENT_STOPPED", f"deployment #{deployment_id} stopped", status="STOPPED", task_id=row.get("task_id"), metadata={"deployment_id": deployment_id})

    def _owns_process(self, row: dict[str, Any]) -> bool:
        """True only if the pid still belongs to the process the controller started (guards against pid reuse)."""
        try:
            proc = psutil.Process(row["pid"])
            return row.get("pid_create_time") is None or abs(proc.create_time() - float(row["pid_create_time"])) < 1.0
        except psutil.NoSuchProcess:
            return False

    def stop_stale(self, port_hint: int | None = None) -> int:
        """Stop deployments left running by a previous (crashed) controller run."""
        stopped = 0
        for row in self.state.active_deployments(self.config.project_id):
            if port_hint and row.get("url") and not row["url"].endswith(f":{port_hint}"):
                continue
            if row.get("pid") and self._owns_process(row):
                kill_process_tree(row["pid"])
                stopped += 1
            self.state.update_deployment(row["id"], status="STOPPED", stopped_at=time.strftime("%Y-%m-%dT%H:%M:%S"), health_status="stopped during recovery")
        return stopped

    def stop_all(self) -> None:
        for row in self.state.active_deployments(self.config.project_id):
            self.stop(row["id"])
