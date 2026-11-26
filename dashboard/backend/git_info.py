"""Read-only git inspection for the Git Management view (no write operations exist here)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

READ_ONLY = {"status", "log", "branch", "rev-parse"}


def _git(path: Path, *args: str) -> str:
    if args[0] not in READ_ONLY:
        raise ValueError("only read-only git commands are available in the dashboard")
    git = shutil.which("git") or "git"
    result = subprocess.run([git, "-c", "core.quotepath=false", *args], cwd=path, capture_output=True, text=True, timeout=20)
    return result.stdout if result.returncode == 0 else ""


def git_summary(workspace: str | None, limit: int = 40) -> dict[str, Any]:
    if not workspace or not Path(workspace).exists():
        return {"available": False, "reason": "workspace not created yet"}
    path = Path(workspace)
    status = [{"code": line[:2], "path": line[3:]} for line in _git(path, "status", "--porcelain=v1").splitlines() if len(line) > 3]
    commits = []
    for line in _git(path, "log", f"-{limit}", "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e").split("\x1e"):
        parts = line.strip().split("\x1f")
        if len(parts) >= 4:
            body = parts[4] if len(parts) > 4 else ""
            task = next((l.split(":", 1)[1].strip() for l in body.splitlines() if l.startswith("Autonomous-Task:")), None)
            commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3], "task_id": task})
    return {
        "available": True,
        "branch": _git(path, "branch", "--show-current").strip(),
        "head": _git(path, "rev-parse", "HEAD").strip(),
        "modified_files": status,
        "commits": commits,
    }
