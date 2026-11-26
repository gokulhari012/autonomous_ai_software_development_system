"""Project configuration, brief, questions and log access for the dashboard.

Settings are edited through an explicit whitelist (key -> type/range). Every write is
validated by the controller itself (``main.py validate-config``) and rolled back if the
controller rejects it, so the dashboard can never leave an invalid or unsafe configuration
behind. Safety switches (production deployment, mandatory review/validation, branch names)
are deliberately not editable here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .settings import Settings

MODEL_RE = re.compile(r"^claude-[a-z0-9.\-]{1,60}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9._-]{1,50}$")


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("must be true or false")


def _int(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not low <= int(value) <= high:
            raise ValueError(f"must be a whole number between {low} and {high}")
        return int(value)
    return check


def _float(low: float, high: float) -> Callable[[Any], float]:
    def check(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
            raise ValueError(f"must be a number between {low} and {high}")
        return float(value)
    return check


def _choice(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in options:
            raise ValueError(f"must be one of {', '.join(options)}")
        return value
    return check


def _text(max_len: int, pattern: re.Pattern | None = None, allow_empty: bool = True) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str) or len(value) > max_len:
            raise ValueError(f"must be text of at most {max_len} characters")
        value = value.strip() if pattern else value
        if not value and allow_empty:
            return value
        if pattern and not pattern.match(value):
            raise ValueError("has an invalid format")
        return value
    return check


def _app_types(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or any(v not in {"flask", "pyside", "python"} for v in value):
        raise ValueError("must be a non-empty list of flask / pyside / python")
    return sorted(set(value))


PROJECT_FIELDS: dict[str, Callable[[Any], Any]] = {
    "project_notes": _text(20000),
    "git_push_enabled": _bool,
    "git_remote": _text(50, REMOTE_RE, allow_empty=False),
    "git_remote_url": _text(300),
    "stop_after": _choice("task", "requirements", "manual"),
    "auto_resume_on_limit": _bool,
    "max_retries_per_task": _int(0, 10),
    "max_tasks_per_run": _int(1, 10000),
    "max_parallel_tasks": _int(1, 4),
    "auto_approve_inferred_requirements": _bool,
    "application_types": _app_types,
    "flask.staging_port": _int(1024, 65535),
    "flask.qa_port": _int(1024, 65535),
    "flask.health_path": _text(100, re.compile(r"^/[A-Za-z0-9/_\-.]*$"), allow_empty=False),
    "pyside.startup_seconds": _int(1, 120),
}

WORKFLOW_FIELDS: dict[str, Callable[[Any], Any]] = {
    "model": _text(64, MODEL_RE, allow_empty=False),
    "fallback_model": _text(64, MODEL_RE),
    "default_effort": _choice("low", "medium", "high", "xhigh", "max"),
    "max_budget_usd": _float(0.1, 100000),
    "max_budget_usd_per_agent_call": _float(0.05, 10000),
    "max_execution_minutes": _int(5, 100000),
    "agent_timeout_seconds": _int(60, 86400),
    "default_max_turns": _int(5, 500),
    "max_agent_turns_per_task": _int(10, 10000),
    "transient_retry_attempts": _int(0, 6),
    "max_stage_errors": _int(1, 5),
}


# The controller's defaults (autonomous-dev/config.py) for keys an older project file may not contain,
# so the dashboard shows the value that is actually in effect.
PROJECT_DEFAULTS: dict[str, Any] = {
    "project_notes": "", "git_push_enabled": False, "git_remote": "origin", "git_remote_url": "", "stop_after": "requirements",
    "auto_resume_on_limit": False, "max_retries_per_task": 3, "max_tasks_per_run": 100, "max_parallel_tasks": 1,
    "auto_approve_inferred_requirements": False, "application_types": [], "flask.staging_port": 5055, "flask.qa_port": 5056,
    "flask.health_path": "/health", "pyside.startup_seconds": 6,
}


class SettingsError(Exception):
    pass


def _get(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


class ProjectAdmin:
    def __init__(self, settings: Settings, run_cli: Callable[..., dict[str, Any]]):
        self.settings = settings
        self.run_cli = run_cli
        self.config_dir = (settings.controller_dir / "config").resolve()

    # ---------------------------------------------------------- paths
    def config_path(self, project: dict[str, Any]) -> Path:
        raw = project.get("config_path")
        if not raw:
            raise SettingsError("this project has no configuration file recorded")
        path = Path(raw).resolve()
        if self.config_dir not in path.parents or path.suffix != ".json":
            raise SettingsError("project configuration must live in autonomous-dev/config")
        return path

    def brief_path(self, project: dict[str, Any]) -> Path:
        data = json.loads(self.config_path(project).read_text(encoding="utf-8"))
        rel = str(data.get("project_brief") or "requirements/brief.md")
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise SettingsError("project_brief must be a relative path inside the project")
        base = Path(project.get("workspace_path") or project["path"]).resolve()
        if not any(root == base or root in base.parents for root in self.settings.allowed_project_roots):
            raise SettingsError(f"{base} is outside the allowed project roots")
        return base / rel

    # ------------------------------------------------------- settings
    def read_settings(self, project: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(self.config_path(project).read_text(encoding="utf-8"))
        workflow = json.loads((self.config_dir / "workflow.json").read_text(encoding="utf-8"))
        return {
            "project": {key: (_get(data, key) if _get(data, key) is not None else PROJECT_DEFAULTS.get(key)) for key in PROJECT_FIELDS},
            "workflow": {key: _get(workflow, key) for key in WORKFLOW_FIELDS},
            "read_only": {key: data.get(key) for key in ("project_name", "project_path", "project_mode", "development_branch", "default_branch",
                                                         "git_isolation", "production_deployment", "require_independent_review", "require_acceptance_validation")},
        }

    def _validated(self, values: dict[str, Any], fields: dict[str, Callable[[Any], Any]]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in fields:
                raise SettingsError(f"{key} cannot be changed from the dashboard")
            try:
                clean[key] = fields[key](value)
            except ValueError as exc:
                raise SettingsError(f"{key} {exc}") from exc
        return clean

    def update_settings(self, project: dict[str, Any], project_values: dict[str, Any], workflow_values: dict[str, Any]) -> dict[str, Any]:
        project_clean = self._validated(project_values or {}, PROJECT_FIELDS)
        workflow_clean = self._validated(workflow_values or {}, WORKFLOW_FIELDS)
        config_path = self.config_path(project)
        workflow_path = self.config_dir / "workflow.json"
        originals = {config_path: config_path.read_text(encoding="utf-8"), workflow_path: workflow_path.read_text(encoding="utf-8")}
        try:
            for path, clean in ((config_path, project_clean), (workflow_path, workflow_clean)):
                if not clean:
                    continue
                data = json.loads(originals[path])
                for key, value in clean.items():
                    _set(data, key, value)
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            check = self.run_cli(str(config_path), ["validate-config"], timeout=60)
            if not check["ok"]:
                raise SettingsError("the controller rejected the settings: " + check["output"][-600:])
        except BaseException:
            for path, text in originals.items():
                path.write_text(text, encoding="utf-8")
            raise
        return self.read_settings(project)

    # ---------------------------------------------------------- brief
    def read_brief(self, project: dict[str, Any]) -> dict[str, Any]:
        path = self.brief_path(project)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        questions = path.parent / "open_questions.md"
        return {"path": str(path), "brief": text, "is_template": "<What problem" in text,
                "open_questions_file": questions.read_text(encoding="utf-8") if questions.exists() else ""}

    def write_brief(self, project: dict[str, Any], text: str) -> dict[str, Any]:
        if not text.strip():
            raise SettingsError("the brief may not be empty")
        if len(text) > 50000:
            raise SettingsError("the brief is limited to 50,000 characters")
        path = self.brief_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        return self.read_brief(project)

    # ----------------------------------------------------------- logs
    def tail_logs(self, lines: int, project_id: str | None, contains: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        sources = {"controller": self.settings.logs_dir / "autonomous-dev.log"}
        if project_id and re.fullmatch(r"[a-z0-9-]{1,80}", project_id):
            sources["last start from dashboard"] = self.settings.logs_dir / f"dashboard-launch-{project_id}.log"
        for label, path in sources.items():
            if not path.exists():
                result[label] = []
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                content = handle.readlines()[-5000:]
            if contains:
                content = [l for l in content if contains.lower() in l.lower()]
            result[label] = [l.rstrip("\n") for l in content[-lines:]]
        return result
