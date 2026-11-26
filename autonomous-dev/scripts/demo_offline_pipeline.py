"""Offline demonstration of the complete workflow WITHOUT Claude API usage.

    python scripts/demo_offline_pipeline.py

Runs the real orchestrator against a fresh copy of the Flask sample (in sample-workspace/demo-...)
with the deterministic *scripted* agent backend, writing to database/demo_state.db. Everything the
orchestrator verifies is real: git worktree + commits, pytest, Playwright, Bandit, Semgrep, pip-audit,
a Flask staging server. The scripted agents:

* add a statistics endpoint that contains a deliberate SQL injection (harmless sample code),
* the scanners catch it, the Bug Fixer repairs it, tests and scans are re-run,
* the Product Validator first makes a false PASS claim, which the orchestrator's own re-run rejects,
* the second validation passes and the task is committed and marked complete.

Every agent run is recorded with backend='scripted'. Point the dashboard at the demo database with
DASHBOARD_DB_PATH=<controller>/database/demo_state.db to explore it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER))

from agent_runner import ScriptedBackend  # noqa: E402
from config import load_config  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from tests.test_orchestrator import _flask_handlers  # noqa: E402  (scripted agent behaviour shared with the tests)


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    workspace_root = CONTROLLER.parent / "sample-workspace"
    target = workspace_root / f"demo-flask-{stamp}"
    shutil.copytree(CONTROLLER / "sample_apps" / "flask-inventory", target, ignore=shutil.ignore_patterns("__pycache__", "*.db", "instance"))
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "Initial version"]):
        subprocess.run(["git", "-c", "user.name=Sample Owner", "-c", "user.email=owner@example.com", *args], cwd=target, check=True, capture_output=True)

    config_file = CONTROLLER / "config" / "projects" / f"demo-flask-{stamp}.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    base = json.loads((CONTROLLER / "config" / "project.json").read_text(encoding="utf-8"))
    base.update({"project_name": f"Offline demo {stamp}", "project_id": f"offline-demo-{stamp}", "project_path": str(target),
                 "project_mode": "existing", "flask": {**base.get("flask", {}), "staging_port": 5065, "qa_port": 5066}})
    config_file.write_text(json.dumps(base, indent=2), encoding="utf-8")

    cfg = load_config(config_file)
    cfg.db_path = str(CONTROLLER / "database" / "demo_state.db")
    setup_logging(CONTROLLER / "logs", "INFO")
    orchestrator = Orchestrator(cfg, backend=ScriptedBackend(_flask_handlers({})))
    summary = asyncio.run(orchestrator.run(max_tasks=5))
    print(json.dumps(summary, indent=2))
    print(f"\nDemo database: {cfg.db_path}\nProject config: {config_file}\nWorkspace (git worktree): {orchestrator.workspace}")
    return 0 if summary["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
