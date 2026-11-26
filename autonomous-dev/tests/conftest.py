"""Shared fixtures for the controller's own test-suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CONTROLLER_DIR = Path(__file__).resolve().parents[1]
if str(CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_DIR))

from safe_exec import CommandPolicy, SafeExecutor  # noqa: E402
from state_manager import StateManager  # noqa: E402

SAMPLE_APPS = CONTROLLER_DIR / "sample_apps"


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    sm = StateManager(tmp_path / "state.db")
    yield sm
    sm.close()


@pytest.fixture
def executor(tmp_path: Path, state: StateManager) -> SafeExecutor:
    policy = CommandPolicy(allowed_roots=[tmp_path], python_executables=[sys.executable])
    return SafeExecutor(policy, state=state)


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False, help="run tests that call the real Claude Agent SDK")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live Claude SDK test; pass --run-live to enable")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def make_config(tmp_path: Path, project_path: Path, **overrides):
    """Build an AppConfig for a test project without touching the real config/project.json."""
    import json

    from config import load_config

    data = {"project_name": overrides.pop("project_name", "Test Project"), "project_path": str(project_path)}
    data.update(overrides)
    cfg_file = tmp_path / "project-config.json"
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    cfg = load_config(cfg_file, env_path=tmp_path / "no.env")
    cfg.db_path = str(tmp_path / "state.db")
    return cfg


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test User", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def user_repo(tmp_path: Path) -> Path:
    """A small 'existing user project' git repository with one commit on main."""
    repo = tmp_path / "user-project"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")
    return repo


def copy_sample(name: str, destination: Path) -> Path:
    target = destination / name
    shutil.copytree(SAMPLE_APPS / name, target, ignore=shutil.ignore_patterns("__pycache__", "*.db", ".pytest_cache"))
    _git(target, "init", "-b", "main")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "initial sample application")
    return target
