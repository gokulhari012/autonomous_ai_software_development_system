"""Configuration loading and validation for the autonomous development controller.

Configuration lives in JSON files under ``config/``:

* ``project.json``  - which application to work on and the global safety switches
* ``workflow.json`` - limits, retry policy, timeouts, agent model settings
* ``testing.json``  - which testing layers are allowed (adapted to the detected project)
* ``security.json`` - scanner settings and severity policy

Secrets are read from ``.env`` (or the process environment) into a ``Secrets``
object that is kept in memory only. They are deliberately *not* exported to
``os.environ`` so that agent sub-processes and test runs never inherit them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CONTROLLER_DIR = Path(__file__).resolve().parent
CONFIG_DIR = CONTROLLER_DIR / "config"
AGENTS_DIR = CONTROLLER_DIR / "agents"
DATABASE_DIR = CONTROLLER_DIR / "database"
LOGS_DIR = CONTROLLER_DIR / "logs"
REPORTS_DIR = CONTROLLER_DIR / "reports"
WORKSPACES_DIR = CONTROLLER_DIR / "workspaces"
BACKUPS_DIR = CONTROLLER_DIR / "backups"
DEFAULT_DB_PATH = DATABASE_DIR / "agent_state.db"

SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


class ConfigError(Exception):
    """Raised for invalid or unsafe configuration."""


def severity_rank(severity: str) -> int:
    try:
        return SEVERITIES.index(severity.upper())
    except ValueError:
        return SEVERITIES.index("MEDIUM")


REMOTE_URL_RE = re.compile(r"^(https://[A-Za-z0-9.\-]+(:\d+)?/[A-Za-z0-9._\-/]+|git@[A-Za-z0-9.\-]+:[A-Za-z0-9._\-/]+|ssh://git@[A-Za-z0-9.\-]+(:\d+)?/[A-Za-z0-9._\-/]+)$")


def valid_remote_url(url: str) -> bool:
    return bool(REMOTE_URL_RE.match(url)) and ".." not in url


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "project"


# ---------------------------------------------------------------------------
# project.json
# ---------------------------------------------------------------------------


class FlaskAppConfig(BaseModel):
    # "module:attribute" understood by ``flask --app``; detected when empty.
    app: str | None = None
    health_path: str = "/health"
    staging_host: str = "127.0.0.1"
    # Opt-in: let other devices on the local network (e.g. a phone) open the staging app. The staging
    # server then listens on all interfaces; browser tests and health checks still use 127.0.0.1.
    lan_access: bool = False
    staging_port: int = 5055
    qa_port: int = 5056
    startup_timeout_seconds: int = 45
    # Optional explicit start command (argv list). Placeholders: {python}, {port}, {host}
    start_command: list[str] | None = None
    # Environment variable through which the app receives its database location.
    database_env_var: str = "DATABASE_URL"
    has_browser_ui: bool | None = None  # None = detect


class PySideAppConfig(BaseModel):
    # Module run with ``python -m`` or a script path relative to the project. Detected when empty.
    entry: str | None = None
    startup_seconds: int = 6
    # Optional packaging command (argv list), e.g. ["{python}", "-m", "PyInstaller", "app.spec"]
    package_command: list[str] | None = None
    # Path (relative to the project) of the built executable, when packaging is used.
    executable_path: str | None = None


# Agents that run for every task. Code review and product validation are mandatory safety gates.
CORE_AGENTS = ("product_manager", "developer", "code_reviewer", "product_validator")
# Agents a project may leave out of its agent team (see agent_selection.py for what replaces each one).
OPTIONAL_AGENTS = ("architect", "qa_engineer", "security_auditor", "bug_fixer", "devops_engineer")


class AgentTeamConfig(BaseModel):
    # full = every agent; custom = the core agents plus ``agents``; auto = sized for each task.
    mode: Literal["full", "custom", "auto"] = "full"
    agents: list[str] = Field(default_factory=lambda: list(OPTIONAL_AGENTS))

    @field_validator("agents")
    @classmethod
    def _known_agents(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(OPTIONAL_AGENTS))
        if unknown:
            raise ValueError(f"agent_team.agents may only contain {list(OPTIONAL_AGENTS)}; unknown: {unknown}")
        return [a for a in OPTIONAL_AGENTS if a in value]


class ProjectConfig(BaseModel):
    project_name: str
    project_path: str
    project_id: str | None = None
    project_mode: Literal["auto", "new", "existing"] = "auto"
    supported_modes: list[Literal["new", "existing"]] = ["new", "existing"]
    require_project_discovery: bool = True
    require_architecture_design: bool = True
    require_acceptance_criteria: bool = True
    application_types: list[str] = Field(default_factory=list)
    default_branch: str = "main"
    development_branch: str = "autonomous-development"
    git_isolation: Literal["worktree", "branch"] = "worktree"
    worktree_dir: str | None = None
    allow_git_init: bool = False
    staging_environment: Literal["local"] = "local"
    deployment_method: Literal["auto", "docker-compose", "process", "desktop"] = "auto"
    max_retries_per_task: int = Field(3, ge=0, le=10)
    max_parallel_agents: int = Field(2, ge=1, le=8)
    max_parallel_tasks: int = Field(1, ge=1, le=4)
    max_tasks_per_run: int = Field(100, ge=1)
    production_deployment: bool = False
    require_git_checkpoint: bool = True
    require_independent_review: bool = True
    require_acceptance_validation: bool = True
    auto_approve_inferred_requirements: bool = False
    # GitHub/remote publishing (opt-in). Only the development branch is ever pushed: never main,
    # never with force. Authentication uses your existing git credentials (credential manager / SSH).
    git_push_enabled: bool = False
    git_remote: str = "origin"
    git_remote_url: str = ""
    # When a run ends: "task" = after each completed task, "requirements" = when no work is left,
    # "manual" = keep running (idle when there is no work) until stopped.
    stop_after: Literal["task", "requirements", "manual"] = "requirements"
    # Wait for the Claude usage limit to reset and continue automatically instead of stopping.
    auto_resume_on_limit: bool = False
    agent_team: AgentTeamConfig = Field(default_factory=AgentTeamConfig)
    # Free-text project description / instructions given to the Product Manager (any mode).
    project_notes: str = Field("", max_length=20000)
    python_executable: str | None = None
    source_dirs: list[str] = Field(default_factory=list)
    test_dir: str = "tests"
    project_brief: str = "requirements/brief.md"
    commit_author_name: str = "Autonomous Dev Agent"
    commit_author_email: str = "autonomous-dev@localhost"
    flask: FlaskAppConfig = Field(default_factory=FlaskAppConfig)
    pyside: PySideAppConfig = Field(default_factory=PySideAppConfig)

    @model_validator(mode="after")
    def _safety(self) -> "ProjectConfig":
        if self.production_deployment:
            raise ConfigError(
                "production_deployment=true is not permitted. Production deployment "
                "requires explicit human authorization and is disabled in this system."
            )
        if self.development_branch.strip() in {self.default_branch, "main", "master"}:
            raise ConfigError("development_branch must differ from the main/default branch.")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", self.development_branch):
            raise ConfigError("development_branch contains unsupported characters.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,50}", self.git_remote):
            raise ConfigError("git_remote must be a simple remote name such as 'origin'.")
        if self.git_remote_url and not valid_remote_url(self.git_remote_url):
            raise ConfigError("git_remote_url must look like https://github.com/<owner>/<repo>.git or git@github.com:<owner>/<repo>.git")
        if self.project_mode != "auto" and self.project_mode not in self.supported_modes:
            raise ConfigError(f"project_mode {self.project_mode!r} is not in supported_modes.")
        if not self.project_id:
            self.project_id = slugify(self.project_name)
        return self


# ---------------------------------------------------------------------------
# workflow.json
# ---------------------------------------------------------------------------


class AgentSettings(BaseModel):
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_turns: int | None = None
    timeout_seconds: int | None = None


class WorkflowConfig(BaseModel):
    model: str | None = "claude-opus-5-5"
    fallback_model: str | None = "claude-sonnet-5-5"
    default_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    default_max_turns: int = 40
    max_agent_turns_per_task: int = 400
    agent_timeout_seconds: int = 1800
    max_execution_minutes: int = 480
    max_budget_usd: float = 25.0
    max_budget_usd_per_agent_call: float = 4.0
    transient_retry_attempts: int = Field(3, ge=0, le=6)
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0
    max_invalid_output_retries: int = Field(1, ge=0, le=3)
    max_stage_errors: int = Field(2, ge=1, le=5)
    lease_seconds: int = 1200
    heartbeat_seconds: int = 30
    review_after_fix: bool = True
    qa_test_fix_attempts: int = Field(1, ge=0, le=3)
    events_retention_days: int = 30
    notification_dedupe_seconds: int = 600
    # OS-level Bash sandboxing offered by Claude Code (macOS/Linux only; ignored on Windows).
    sandbox_bash: bool = True
    # Pick each agent call's model and effort from the task's complexity (ignores per-agent model/effort).
    auto_model_selection: bool = False
    # When an agent fails a task (error, invalid output, or its work is sent back for repair), retry it
    # one step stronger: higher effort first, then a stronger model, never above escalation_max_model.
    auto_escalation: bool = False
    escalation_max_model: Literal["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"] = "claude-opus-5-5"
    agents: dict[str, AgentSettings] = Field(default_factory=dict)

    def agent(self, name: str) -> AgentSettings:
        return self.agents.get(name, AgentSettings())


# ---------------------------------------------------------------------------
# testing.json
# ---------------------------------------------------------------------------


class PythonTestingConfig(BaseModel):
    enabled: bool = True
    framework: Literal["pytest"] = "pytest"
    timeout_seconds: int = 900
    per_test_timeout_seconds: int = 120
    extra_args: list[str] = Field(default_factory=list)


class FlaskTestingConfig(BaseModel):
    api_testing: bool = True
    database_testing: bool = True
    integration_testing: bool = True
    playwright: bool = True
    browsers: list[str] = ["chromium"]


class PySideTestingConfig(BaseModel):
    enabled: bool = True
    pytest_qt: bool = True
    pyautogui: bool = True
    packaged_application_testing: bool = True
    offscreen_widget_tests: bool = True
    # PyAutoGUI moves the real mouse and keyboard. It only runs when explicitly allowed,
    # ideally on a dedicated test desktop; otherwise GUI tests are reported BLOCKED.
    allow_real_input_automation: bool = False


class SecurityToggles(BaseModel):
    bandit: bool = True
    semgrep: bool = True
    pip_audit: bool = True


class ReportToggles(BaseModel):
    json_reports: bool = Field(True, alias="json")
    junit_xml: bool = True
    screenshots: bool = True

    model_config = {"populate_by_name": True}


class TestingConfig(BaseModel):
    python: PythonTestingConfig = Field(default_factory=PythonTestingConfig)
    flask: FlaskTestingConfig = Field(default_factory=FlaskTestingConfig)
    pyside: PySideTestingConfig = Field(default_factory=PySideTestingConfig)
    security: SecurityToggles = Field(default_factory=SecurityToggles)
    reports: ReportToggles = Field(default_factory=ReportToggles)


# ---------------------------------------------------------------------------
# security.json
# ---------------------------------------------------------------------------


class SemgrepConfig(BaseModel):
    # Registry rulesets need network access (no login needed). "auto" is not used
    # because it requires sending metrics to the registry.
    registry_configs: list[str] = ["p/python", "p/flask", "p/security-audit", "p/secrets"]
    use_registry: bool = True
    local_rules: str = "config/semgrep/local-rules.yml"
    timeout_seconds: int = 900


class BanditConfig(BaseModel):
    exclude_dirs: list[str] = ["tests", ".venv", "venv", "node_modules", "build", "dist"]
    timeout_seconds: int = 600


class PipAuditConfig(BaseModel):
    timeout_seconds: int = 900
    vulnerability_severity: str = "HIGH"


class SecurityConfig(BaseModel):
    block_severity_threshold: str = "HIGH"
    auditor_may_dismiss_severities: list[str] = ["INFO", "LOW", "MEDIUM"]
    scanner_failure_policy: dict[str, Literal["block", "warn"]] = {
        "bandit": "block",
        "semgrep": "block",
        "pip_audit": "warn",
    }
    block_preexisting_critical: bool = True
    exceptions_file: str = "config/security_exceptions.json"
    bandit: BanditConfig = Field(default_factory=BanditConfig)
    semgrep: SemgrepConfig = Field(default_factory=SemgrepConfig)
    pip_audit: PipAuditConfig = Field(default_factory=PipAuditConfig)

    @field_validator("block_severity_threshold")
    @classmethod
    def _valid_threshold(cls, v: str) -> str:
        if v.upper() not in SEVERITIES:
            raise ValueError(f"block_severity_threshold must be one of {SEVERITIES}")
        return v.upper()


# ---------------------------------------------------------------------------
# Secrets (.env)
# ---------------------------------------------------------------------------


class Secrets(BaseModel):
    anthropic_api_key: str | None = None
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_authorized_user_ids: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    db_path: str | None = None

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs, events or Telegram messages."""
        return [v for v in (self.anthropic_api_key, self.telegram_bot_token) if v and len(v) >= 8]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_secrets(env_path: Path | None = None) -> Secrets:
    file_values = parse_env_file(env_path or CONTROLLER_DIR / ".env")

    def get(name: str) -> str | None:
        value = os.environ.get(name) or file_values.get(name)
        return value.strip() if value and value.strip() else None

    token = get("TELEGRAM_BOT_TOKEN")
    enabled_raw = (get("TELEGRAM_ENABLED") or "true").lower()
    users = get("TELEGRAM_AUTHORIZED_USER_IDS") or ""
    return Secrets(
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        telegram_enabled=bool(token) and enabled_raw in {"1", "true", "yes", "on"},
        telegram_bot_token=token,
        telegram_chat_id=get("TELEGRAM_CHAT_ID"),
        telegram_authorized_user_ids=[u.strip() for u in users.split(",") if u.strip()],
        log_level=(get("AUTODEV_LOG_LEVEL") or "INFO").upper(),
        db_path=get("AUTODEV_DB_PATH"),
    )


# ---------------------------------------------------------------------------
# Aggregate application config
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    project: ProjectConfig
    workflow: WorkflowConfig
    testing: TestingConfig
    security: SecurityConfig
    secrets: Secrets
    config_path: str
    db_path: str

    model_config = {"arbitrary_types_allowed": True}

    @property
    def project_id(self) -> str:
        assert self.project.project_id
        return self.project.project_id

    @property
    def project_dir(self) -> Path:
        return resolve_path(self.project.project_path)

    @property
    def reports_dir(self) -> Path:
        return REPORTS_DIR

    def worktree_path(self) -> Path:
        if self.project.worktree_dir:
            return resolve_path(self.project.worktree_dir)
        return WORKSPACES_DIR / self.project_id


def resolve_path(value: str | Path, base: Path = CONTROLLER_DIR) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _read_json(path: Path, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Missing configuration file: {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc


def load_config(project_config_path: str | Path | None = None, env_path: Path | None = None) -> AppConfig:
    """Load and validate all configuration.

    ``project_config_path`` may point at any project JSON (e.g. a project added
    through the dashboard under ``config/projects/``). The shared workflow,
    testing and security files always come from ``config/``.
    """
    project_path = resolve_path(project_config_path or CONFIG_DIR / "project.json")
    project_data = _read_json(project_path, required=True)
    try:
        project = ProjectConfig(**project_data)
        workflow = WorkflowConfig(**_read_json(CONFIG_DIR / "workflow.json", required=False))
        testing = TestingConfig(**_read_json(CONFIG_DIR / "testing.json", required=False))
        security = SecurityConfig(**_read_json(CONFIG_DIR / "security.json", required=False))
    except ConfigError:
        raise
    except Exception as exc:  # pydantic.ValidationError and friends
        raise ConfigError(f"Configuration validation failed: {exc}") from exc
    secrets = load_secrets(env_path)
    db_path = resolve_path(secrets.db_path) if secrets.db_path else DEFAULT_DB_PATH
    return AppConfig(
        project=project,
        workflow=workflow,
        testing=testing,
        security=security,
        secrets=secrets,
        config_path=str(project_path),
        db_path=str(db_path),
    )


def ensure_runtime_dirs() -> None:
    for directory in (
        DATABASE_DIR,
        LOGS_DIR,
        REPORTS_DIR / "reviews",
        REPORTS_DIR / "tests",
        REPORTS_DIR / "security",
        REPORTS_DIR / "releases",
        REPORTS_DIR / "staging",
        WORKSPACES_DIR,
        BACKUPS_DIR,
        CONFIG_DIR / "projects",
    ):
        directory.mkdir(parents=True, exist_ok=True)
