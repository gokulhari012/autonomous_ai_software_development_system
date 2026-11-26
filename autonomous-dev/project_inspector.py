"""Deterministic project inspection (the PROJECT_SCAN stage).

Detects the technology stack from the files that actually exist - it never
assumes a directory layout. The result (:class:`ProjectProfile`) drives which
testing layers, scanners and deployment method are used.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "dist", "build", ".tox", ".idea", ".vscode", "site-packages", ".staging", ".autodev",
}
DOC_NAMES = {"readme.md", "readme.rst", "readme.txt", "claude.md", "changelog.md", "contributing.md"}
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt", "requirements/base.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile")

FLASK_IMPORT = re.compile(r"^\s*(from\s+flask\s+import|import\s+flask\b)", re.M)
FLASK_APP = re.compile(r"\bFlask\s*\(\s*__name__")
CREATE_APP = re.compile(r"^def\s+create_app\s*\(", re.M)
RENDER_TEMPLATE = re.compile(r"\brender_template(_string)?\s*\(")
PYSIDE_IMPORT = re.compile(r"^\s*(from|import)\s+(PySide6|PySide2)\b", re.M)
PYQT_IMPORT = re.compile(r"^\s*(from|import)\s+(PyQt5|PyQt6)\b", re.M)
QAPP = re.compile(r"\bQApplication\s*\(")
MAIN_GUARD = re.compile(r"if\s+__name__\s*==\s*['\"]__main__['\"]")
SQLITE = re.compile(r"\bsqlite3\b|sqlite:///")
SQLALCHEMY = re.compile(r"\bsqlalchemy\b|flask_sqlalchemy", re.I)
POSTGRES = re.compile(r"psycopg|postgresql://|asyncpg", re.I)
MYSQL = re.compile(r"pymysql|mysqlclient|mysql://|mysql\+", re.I)


class ProjectProfile(BaseModel):
    project_dir: str
    exists: bool = True
    is_empty: bool = False
    detected_mode: Literal["new", "existing", "ambiguous"] = "existing"
    python_files: int = 0
    total_files: int = 0
    application_types: list[str] = Field(default_factory=list)  # flask, pyside, python
    flask_app: str | None = None
    flask_app_file: str | None = None
    has_browser_ui: bool = False
    templates_dirs: list[str] = Field(default_factory=list)
    pyside_version: str | None = None
    pyside_entry: str | None = None
    uses_pyqt: bool = False
    databases: list[str] = Field(default_factory=list)
    migrations_dir: str | None = None
    source_dirs: list[str] = Field(default_factory=list)
    test_dirs: list[str] = Field(default_factory=list)
    test_files: int = 0
    has_conftest: bool = False
    pytest_config: str | None = None
    requirement_files: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    requirement_docs: list[str] = Field(default_factory=list)
    has_claude_md: bool = False
    dockerfile: bool = False
    compose_files: list[str] = Field(default_factory=list)
    packaging: list[str] = Field(default_factory=list)
    health_endpoint: bool = False
    notes: list[str] = Field(default_factory=list)


def _iter_files(root: Path, limit: int = 20000):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") or d == ".github"]
        for name in filenames:
            count += 1
            if count > limit:
                return
            yield Path(dirpath) / name


def _read(path: Path, max_bytes: int = 400_000) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _module_name(root: Path, file_path: Path) -> str:
    rel = file_path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def inspect_project(project_dir: Path, brief_rel: str = "requirements/brief.md") -> ProjectProfile:
    profile = ProjectProfile(project_dir=str(project_dir))
    if not project_dir.exists():
        profile.exists = False
        profile.is_empty = True
        profile.detected_mode = "new"
        profile.notes.append("Project directory does not exist yet.")
        return profile

    py_files: list[Path] = []
    all_files: list[Path] = []
    for file_path in _iter_files(project_dir):
        rel = file_path.relative_to(project_dir)
        all_files.append(rel)
        name = file_path.name.lower()
        rel_posix = rel.as_posix()
        if file_path.suffix == ".py":
            py_files.append(file_path)
            if name.startswith("test_") or name.endswith("_test.py"):
                profile.test_files += 1
            if name == "conftest.py":
                profile.has_conftest = True
        if name in DOC_NAMES or (rel.parts and rel.parts[0].lower() == "docs" and file_path.suffix in {".md", ".rst", ".txt"}):
            profile.docs.append(rel_posix)
        if name == "claude.md":
            profile.has_claude_md = True
        if rel.parts and rel.parts[0].lower() in {"requirements", "specs", "spec"} and file_path.suffix in {".md", ".txt", ".rst"}:
            profile.requirement_docs.append(rel_posix)
        if rel_posix in REQUIREMENT_FILES or (name.startswith("requirements") and name.endswith(".txt")):
            profile.requirement_files.append(rel_posix)
        if name == "dockerfile":
            profile.dockerfile = True
        if name.startswith("docker-compose") or name in {"compose.yml", "compose.yaml"}:
            profile.compose_files.append(rel_posix)
        if file_path.suffix == ".spec" or name in {"setup.iss"}:
            profile.packaging.append(rel_posix)
        if name in {"pytest.ini", "tox.ini"}:
            profile.pytest_config = rel_posix
        if name.endswith(".html") and "templates" in {p.lower() for p in rel.parts}:
            tdir = "/".join(rel.parts[: [p.lower() for p in rel.parts].index("templates") + 1])
            if tdir not in profile.templates_dirs:
                profile.templates_dirs.append(tdir)
        if rel.parts and rel.parts[-1] == "env.py" and "migrations" in rel.parts:
            profile.migrations_dir = "/".join(rel.parts[:-1])

    profile.total_files = len(all_files)
    profile.python_files = len(py_files)
    meaningful = [f for f in all_files if f.as_posix() != brief_rel and f.name.lower() not in {".gitignore", ".ds_store", "readme.md"}]
    if not py_files and len(meaningful) <= 3:
        profile.is_empty = True
        profile.detected_mode = "new"
    elif not py_files:
        profile.detected_mode = "ambiguous"
        profile.notes.append("Files exist but no Python sources were found.")
    else:
        profile.detected_mode = "existing"

    requirements_text = " ".join(_read(project_dir / r).lower() for r in profile.requirement_files)
    if (project_dir / "pyproject.toml").exists():
        pyproject = _read(project_dir / "pyproject.toml")
        if "[tool.pytest" in pyproject:
            profile.pytest_config = profile.pytest_config or "pyproject.toml"

    flask_candidates: list[tuple[int, Path, str]] = []
    pyside_versions: set[str] = set()
    pyside_entries: list[Path] = []
    dbs: set[str] = set()
    source_roots: set[str] = set()
    test_dirs: set[str] = set()
    for file_path in py_files:
        rel = file_path.relative_to(project_dir)
        is_test = any(p.lower() in {"tests", "test"} for p in rel.parts[:-1]) or rel.name.startswith("test_")
        if is_test:
            for i, part in enumerate(rel.parts[:-1]):
                if part.lower() in {"tests", "test"}:
                    test_dirs.add("/".join(rel.parts[: i + 1]))
                    break
            continue
        text = _read(file_path)
        if not text:
            continue
        top = rel.parts[0] if len(rel.parts) > 1 else "."
        source_roots.add(top)
        if FLASK_IMPORT.search(text) and FLASK_APP.search(text):
            score = 2 if CREATE_APP.search(text) else 1
            attr = "create_app" if CREATE_APP.search(text) else "app"
            flask_candidates.append((score, file_path, attr))
        if RENDER_TEMPLATE.search(text):
            profile.has_browser_ui = True
        if "/health" in text:
            profile.health_endpoint = True
        match = PYSIDE_IMPORT.search(text)
        if match:
            pyside_versions.add(match.group(2))
            if QAPP.search(text) and MAIN_GUARD.search(text):
                pyside_entries.append(file_path)
        if PYQT_IMPORT.search(text):
            profile.uses_pyqt = True
        if SQLITE.search(text):
            dbs.add("sqlite")
        if SQLALCHEMY.search(text):
            dbs.add("sqlalchemy")
        if POSTGRES.search(text):
            dbs.add("postgresql")
        if MYSQL.search(text):
            dbs.add("mysql")

    if "flask" in requirements_text and not flask_candidates:
        profile.notes.append("Flask is listed in requirements but no Flask(__name__) application was found.")
    if flask_candidates:
        flask_candidates.sort(key=lambda c: (-c[0], len(c[1].parts)))
        _, app_file, attr = flask_candidates[0]
        module = _module_name(project_dir, app_file)
        profile.flask_app = f"{module}:{attr}" + ("()" if attr == "create_app" else "")
        profile.flask_app_file = app_file.relative_to(project_dir).as_posix()
        profile.application_types.append("flask")
    if profile.templates_dirs:
        profile.has_browser_ui = True
    if pyside_versions or "pyside6" in requirements_text or "pyside2" in requirements_text:
        version = "PySide6" if "PySide6" in pyside_versions or "pyside6" in requirements_text else "PySide2"
        profile.pyside_version = version
        profile.application_types.append("pyside")
        if pyside_entries:
            entry = sorted(pyside_entries, key=lambda p: len(p.parts))[0]
            module = _module_name(project_dir, entry)
            profile.pyside_entry = module
    if py_files and not profile.application_types:
        profile.application_types.append("python")
    for name in ("psycopg", "pymysql", "sqlalchemy"):
        if name in requirements_text:
            dbs.add({"psycopg": "postgresql", "pymysql": "mysql"}.get(name, name))
    profile.databases = sorted(dbs)

    candidate_sources = []
    if (project_dir / "src").is_dir():
        candidate_sources.append("src")
    for root in sorted(source_roots):
        if root == "." or root in {"src", "tests", "test", "docs", "scripts", "migrations"}:
            continue
        if (project_dir / root).is_dir():
            candidate_sources.append(root)
    if "." in source_roots and not candidate_sources:
        candidate_sources.append(".")
    profile.source_dirs = candidate_sources or (["."] if py_files else [])
    profile.test_dirs = sorted(test_dirs)
    if profile.has_claude_md:
        profile.notes.append("CLAUDE.md found; its conventions are passed to every agent.")
    return profile


def gui_session_available() -> tuple[bool, str]:
    """Whether real (non-offscreen) GUI automation can run in this session."""
    if os.name == "nt":
        session = os.environ.get("SESSIONNAME", "")
        if session.lower().startswith("services") or not session:
            # Services session 0 has no interactive desktop.
            try:
                import ctypes

                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                if user32.GetDesktopWindow() and user32.GetSystemMetrics(0) > 0:
                    return True, "interactive Windows desktop detected"
            except Exception:
                pass
            return False, "no interactive Windows desktop session"
        return True, f"interactive Windows session {session!r}"
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True, "X11/Wayland display available"
    import sys

    if sys.platform == "darwin":
        return True, "macOS session"
    return False, "no DISPLAY or WAYLAND_DISPLAY set (use xvfb-run for a virtual display)"
