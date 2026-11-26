"""Bridge from dashboard controls to the controller CLI.

Every action maps to a fixed argument vector of ``autonomous-dev/main.py``; no
user-supplied string is ever interpreted by a shell, and no git command or
arbitrary command can be triggered from the dashboard.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .settings import Settings

NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,80}$")
CODE_RE = re.compile(r"^REQ-\d{1,5}$", re.I)


class ControlError(Exception):
    pass


class ControllerBridge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.python = settings.python_executable or self._default_python()

    def _default_python(self) -> str:
        venv = self.settings.controller_dir.parent / ".venv"
        candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return str(candidate) if candidate.exists() else sys.executable

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in [k for k in env if k.startswith("DASHBOARD_")]:
            env.pop(key)  # never pass dashboard secrets to the controller
        env["AUTODEV_DB_PATH"] = str(self.settings.db_path)  # the controller uses the database this dashboard shows
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _base(self, config_path: str | None) -> list[str]:
        argv = [self.python, str(self.settings.controller_dir / "main.py")]
        if config_path:
            path = Path(config_path).resolve()
            config_root = (self.settings.controller_dir / "config").resolve()
            if path.suffix != ".json" or (config_root not in path.parents and path != config_root / "project.json"):
                raise ControlError("project configuration must live in autonomous-dev/config")
            argv += ["--config", str(path)]
        return argv + ["--quiet"]

    def run_cli(self, config_path: str | None, args: list[str], timeout: int = 120, stdin_text: str | None = None) -> dict[str, Any]:
        argv = self._base(config_path) + args
        try:
            result = subprocess.run(argv, cwd=self.settings.controller_dir, capture_output=True, text=True, timeout=timeout,
                                    env=self._env(), input=stdin_text, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": None, "output": f"timed out after {timeout}s"}
        return {"ok": result.returncode == 0, "exit_code": result.returncode, "output": (result.stdout + result.stderr)[-6000:]}

    def start(self, config_path: str | None, project_id: str, with_telegram_bot: bool, max_tasks: int | None = None) -> dict[str, Any]:
        argv = self._base(config_path) + ["run"] + (["--with-telegram-bot"] if with_telegram_bot else [])
        if max_tasks:
            argv += ["--max-tasks", str(int(max_tasks))]
        log_path = self.settings.logs_dir / f"dashboard-launch-{project_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "ab") as handle:
            proc = subprocess.Popen(argv, cwd=self.settings.controller_dir, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=self._env(), **kwargs)
        return {"ok": True, "pid": proc.pid, "log": str(log_path)}

    def validate_project_path(self, path_value: str, must_exist: bool) -> Path:
        path = Path(path_value).expanduser().resolve()
        if not any(root == path or root in path.parents for root in self.settings.allowed_project_roots):
            raise ControlError(f"{path} is outside the allowed project roots {[str(r) for r in self.settings.allowed_project_roots]}")
        if must_exist and not path.is_dir():
            raise ControlError(f"{path} is not an existing directory")
        if self.settings.controller_dir == path or self.settings.controller_dir in path.parents:
            raise ControlError("the controller directory itself cannot be a target project")
        return path

    def add_project(self, name: str, path_value: str, mode: str) -> dict[str, Any]:
        if not NAME_RE.match(name):
            raise ControlError("project name may contain letters, digits, spaces, '.', '_' and '-' only")
        if mode not in {"new", "existing", "auto"}:
            raise ControlError("invalid mode")
        path = self.validate_project_path(path_value, must_exist=mode == "existing")
        return self.run_cli(None, ["project", "add", "--name", name, "--path", str(path), "--mode", mode])

    @staticmethod
    def validate_code(code: str) -> str:
        if not CODE_RE.match(code):
            raise ControlError("invalid requirement code")
        return code.upper()
