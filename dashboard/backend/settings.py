"""Dashboard settings (environment variables or dashboard/.env)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = DASHBOARD_DIR.parent
DEFAULT_CONTROLLER_DIR = REPO_DIR / "autonomous-dev"
SECRET_FILE = DASHBOARD_DIR / "backend" / ".dashboard_secret"


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    allow_remote: bool = False
    controller_dir: Path = DEFAULT_CONTROLLER_DIR
    db_path: Path = DEFAULT_CONTROLLER_DIR / "database" / "agent_state.db"
    username: str = "admin"
    password_hash: str | None = None
    secret_key: str = ""
    session_hours: int = 8
    allowed_project_roots: list[Path] = field(default_factory=list)
    python_executable: str | None = None
    secure_cookies: bool = False

    @property
    def reports_dir(self) -> Path:
        return self.controller_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.controller_dir / "logs"


def _secret_key() -> str:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


def load_settings(**overrides) -> Settings:
    file_values = _env_file_values(DASHBOARD_DIR / ".env")

    def get(name: str, default: str | None = None) -> str | None:
        return os.environ.get(name) or file_values.get(name) or default

    controller_dir = Path(get("DASHBOARD_CONTROLLER_DIR", str(DEFAULT_CONTROLLER_DIR))).resolve()
    roots = get("DASHBOARD_ALLOWED_PROJECT_ROOTS", str(REPO_DIR.parent)) or ""
    settings = Settings(
        host=get("DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(get("DASHBOARD_PORT", "8000") or 8000),
        allow_remote=(get("DASHBOARD_ALLOW_REMOTE", "false") or "").lower() in {"1", "true", "yes"},
        controller_dir=controller_dir,
        db_path=Path(get("DASHBOARD_DB_PATH", str(controller_dir / "database" / "agent_state.db"))).resolve(),
        username=get("DASHBOARD_USERNAME", "admin") or "admin",
        password_hash=get("DASHBOARD_PASSWORD_HASH"),
        secret_key=get("DASHBOARD_SECRET_KEY") or _secret_key(),
        session_hours=int(get("DASHBOARD_SESSION_HOURS", "8") or 8),
        allowed_project_roots=[Path(r).resolve() for r in roots.split(",") if r.strip()],
        python_executable=get("DASHBOARD_CONTROLLER_PYTHON"),
        secure_cookies=(get("DASHBOARD_SECURE_COOKIES", "false") or "").lower() in {"1", "true", "yes"},
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    if settings.host not in {"127.0.0.1", "localhost", "::1"} and not settings.allow_remote:
        raise RuntimeError(
            f"Refusing to bind the dashboard to {settings.host}. It is local-only by default; set DASHBOARD_ALLOW_REMOTE=true "
            "only behind additional security (TLS, reverse proxy, firewall)."
        )
    return settings
