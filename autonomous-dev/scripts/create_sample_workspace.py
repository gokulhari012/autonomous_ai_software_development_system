"""Create git repositories from the sample applications to act as "existing projects".

    python scripts/create_sample_workspace.py

Creates ../sample-workspace/flask-inventory and ../sample-workspace/pyside-notes (each a git
repository with one commit on main, like a real user project) and registers a project config
for the PySide sample in config/projects/. config/project.json already points at the Flask one.
Existing sample repositories are left untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
WORKSPACE = CONTROLLER.parent / "sample-workspace"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=Sample Owner", "-c", "user.email=owner@example.com", *args], cwd=cwd, check=True, capture_output=True)


def create(name: str) -> Path:
    target = WORKSPACE / name
    if target.exists():
        print(f"exists, left untouched: {target}")
        return target
    shutil.copytree(CONTROLLER / "sample_apps" / name, target, ignore=shutil.ignore_patterns("__pycache__", "*.db", ".pytest_cache", "instance"))
    git(target, "init", "-b", "main")
    git(target, "add", "-A")
    git(target, "commit", "-m", "Initial version of the sample application")
    print(f"created {target}")
    return target


def main() -> int:
    WORKSPACE.mkdir(exist_ok=True)
    create("flask-inventory")
    pyside = create("pyside-notes")
    base = json.loads((CONTROLLER / "config" / "project.json").read_text(encoding="utf-8"))
    base.update({"project_name": "PySide Notes Sample", "project_path": str(pyside), "project_id": "pyside-notes-sample", "project_mode": "existing", "application_types": ["pyside"]})
    out = CONTROLLER / "config" / "projects" / "pyside-notes-sample.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(base, indent=2), encoding="utf-8")
    print(f"project config for the PySide sample: {out}")
    print("\nNext: python main.py doctor && python main.py scan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
