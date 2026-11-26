"""Phase 3 + Phase 8: the orchestrator state machine end-to-end.

The agents are replaced by the deterministic ScriptedBackend (no Claude calls), but
everything the orchestrator verifies independently is REAL: git worktree and commits,
pytest runs, Playwright (Chromium) browser tests, pytest-qt, Bandit, Semgrep, pip-audit,
a real Flask staging server, a real PySide launch, and the Telegram queue (mock Bot API).
Scripted file edits go through the same ToolGuard permission checks as real agents.
"""

from __future__ import annotations

import asyncio
import re
import subprocess

import psutil
import pytest

from agent_models import ProductManagerOutput
from agent_runner import ScriptedBackend
from orchestrator import Orchestrator, Stage, StageOutcome
from state_manager import StateManager
from task_manager import TaskManager
from tests.conftest import copy_sample, make_config
from tests.test_testing_security_deploy import FakeTelegram

CRITERIA = [
    "GET /api/items/stats returns 200 with JSON keys count and total_quantity",
    "The counts reflect the items stored in the database",
]
VULNERABLE_STATS = '''

@api_bp.get("/items/stats")
def item_stats():
    from .db import get_db

    name_filter = request.args.get("name", "")
    row = get_db().execute(f"SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM items WHERE name LIKE '%{name_filter}%'").fetchone()
    return jsonify({"count": row[0], "total_quantity": row[1]})
'''
VULN_LINE = "row = get_db().execute(f\"SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM items WHERE name LIKE '%{name_filter}%'\").fetchone()"
SAFE_LINE = 'row = get_db().execute("SELECT COUNT(*), COALESCE(SUM(quantity), 0) FROM items WHERE name LIKE ?", (f"%{name_filter}%",)).fetchone()'
QA_TESTS = '''def test_stats_counts(client):
    client.post("/api/items", json={"sku": "A", "name": "Anvil", "quantity": 2})
    client.post("/api/items", json={"sku": "B", "name": "Bolt", "quantity": 5})
    assert client.get("/api/items/stats").get_json() == {"count": 2, "total_quantity": 7}


def test_stats_empty(client):
    assert client.get("/api/items/stats").get_json() == {"count": 0, "total_quantity": 0}
'''
REGRESSION_TEST = '''

def test_stats_name_filter_is_parameterised(client):
    client.post("/api/items", json={"sku": "A", "name": "Anvil", "quantity": 2})
    response = client.get("/api/items/stats?name=' OR 1=1 --")
    assert response.status_code == 200 and response.get_json()["count"] == 0
'''
ACCEPTANCE_WRONG = '''import json
import os
import urllib.request

BASE = os.environ["APP_BASE_URL"]


def test_stats_endpoint_returns_counts():
    with urllib.request.urlopen(BASE + "/api/items/stats") as response:
        data = json.loads(response.read())
    assert "total" in data  # wrong key: the validator's claim of PASS is false
'''
ACCEPTANCE_RIGHT = '''import json
import os
import urllib.request
import uuid

BASE = os.environ["APP_BASE_URL"]


def _get(path):
    with urllib.request.urlopen(BASE + path) as response:
        return response.status, json.loads(response.read())


def _post(path, payload):
    request = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        return response.status


def test_stats_endpoint_returns_counts():
    status, data = _get("/api/items/stats")
    assert status == 200 and set(data) == {"count", "total_quantity"}


def test_stats_reflect_database():
    _, before = _get("/api/items/stats")
    assert _post("/api/items", {"sku": uuid.uuid4().hex[:8], "name": "Acceptance", "quantity": 4}) == 201
    _, after = _get("/api/items/stats")
    assert after["count"] == before["count"] + 1 and after["total_quantity"] == before["total_quantity"] + 4
'''


def _pm_output(include_inferred: bool = True) -> dict:
    requirements = [{
        "title": "Inventory statistics endpoint", "description": "GET /api/items/stats returns item count and total quantity",
        "priority": 1, "acceptance_criteria": CRITERIA, "source": "documented", "source_reference": "requirements/requirements.md",
    }]
    if include_inferred:
        requirements.append({"title": "Customer management", "description": "Manage customers", "priority": 2,
                             "acceptance_criteria": ["Customers can be created"], "source": "inferred"})
    return {
        "status": "PASS", "summary": "analysis complete", "project_purpose": "Inventory management",
        "existing_features": ["items CRUD"], "requirements": requirements,
        "tasks": [{"title": "Add item statistics endpoint", "description": "Add GET /api/items/stats", "requirement_title": "Inventory statistics endpoint",
                   "priority": 1, "kind": "feature", "acceptance_criteria": CRITERIA}],
    }


def _architect(call):
    return {"status": "PASS", "summary": "design ready", "approach": "Add a stats route to the api blueprint using one aggregate query.",
            "files_to_modify": ["inventory/api.py"], "files_to_create": ["tests/test_stats.py"], "test_plan": ["API tests for /api/items/stats"]}


def _reviewer_pass(call):
    return {"status": "PASS", "summary": "looks correct", "acceptance_criteria_met": [{"criterion": c, "met": True, "evidence": "route present"} for c in CRITERIA]}


def _issue_ids(prompt: str) -> list[int]:
    return [int(x) for x in re.findall(r"\[issue_id (\d+)\]", prompt)]


def _flask_handlers(state_holder: dict) -> dict:
    def developer(call):
        call.write_file("inventory/api.py", call.read_file("inventory/api.py") + VULNERABLE_STATS)
        return {"status": "PASS", "summary": "stats endpoint added", "modified_files": ["inventory/api.py"]}

    def qa_generate(call):
        call.write_file("tests/test_stats.py", QA_TESTS)
        return {"status": "PASS", "summary": "tests written", "project_type_detected": "flask", "test_files_written": ["tests/test_stats.py"],
                "test_plan": [{"criterion": CRITERIA[0], "layer": "api", "test_ids": ["tests/test_stats.py::test_stats_empty"]},
                              {"criterion": CRITERIA[1], "layer": "api", "test_ids": ["tests/test_stats.py::test_stats_counts"]}]}

    def auditor(call):
        ids = [int(x) for x in re.findall(r"finding_id (\d+)", call.prompt)]
        return {"status": "FAIL" if ids else "PASS", "summary": f"{len(ids)} finding(s) assessed",
                "assessments": [{"finding_id": i, "verdict": "VERIFIED", "evidence": "f-string interpolates request input into SQL (inventory/api.py)"} for i in ids],
                "manual_checks_performed": ["input validation", "sql construction"]}

    def fix_security(call):
        call.replace_in_file("inventory/api.py", VULN_LINE, SAFE_LINE)
        call.write_file("tests/test_stats.py", call.read_file("tests/test_stats.py") + REGRESSION_TEST)
        return {"status": "PASS", "summary": "parameterised the query",
                "fixes": [{"issue_id": i, "root_cause": "f-string SQL", "fix_description": "use a placeholder", "files_modified": ["inventory/api.py"],
                           "regression_test": "tests/test_stats.py::test_stats_name_filter_is_parameterised", "reproduced": True} for i in _issue_ids(call.prompt)]}

    def fix_after_validation(call):
        return {"status": "PASS", "summary": "application matches the specification; the failing acceptance test expected a non-existent key",
                "fixes": [], "unresolved_issue_ids": _issue_ids(call.prompt)}

    def validator_wrong(call):
        call.write_file(f"tests/acceptance/test_task_{call.ctx.request.task_id}_stats.py", ACCEPTANCE_WRONG)
        node = f"tests/acceptance/test_task_{call.ctx.request.task_id}_stats.py::test_stats_endpoint_returns_counts"
        return {"status": "PASS", "summary": "all good (claimed)", "recommend_completion": True,
                "criteria_results": [{"criterion": c, "verdict": "PASS", "evidence_test_ids": [node]} for c in CRITERIA]}

    def validator_right(call):
        path = f"tests/acceptance/test_task_{call.ctx.request.task_id}_stats.py"
        call.write_file(path, ACCEPTANCE_RIGHT)
        return {"status": "PASS", "summary": "validated against staging", "recommend_completion": True, "acceptance_test_files": [path],
                "criteria_results": [{"criterion": CRITERIA[0], "verdict": "PASS", "evidence_test_ids": [f"{path}::test_stats_endpoint_returns_counts"]},
                                     {"criterion": CRITERIA[1], "verdict": "PASS", "evidence_test_ids": [f"{path}::test_stats_reflect_database"]}]}

    return {
        "product_manager": lambda call: _pm_output(),
        "architect": _architect,
        "developer": developer,
        "code_reviewer": _reviewer_pass,
        "qa_engineer": [qa_generate],
        "security_auditor": auditor,
        "bug_fixer": [fix_security, fix_after_validation],
        "product_validator": [validator_wrong, validator_right],
    }


def _orchestrator(tmp_path, project, handlers, **overrides):
    overrides.setdefault("flask", {"staging_port": 5195, "qa_port": 5196, "startup_timeout_seconds": 30})
    cfg = make_config(tmp_path, project, project_name=f"orch-{tmp_path.name}"[:40], worktree_dir=str(tmp_path / "wt"), **overrides)
    cfg.security.semgrep.use_registry = False
    cfg.workflow.heartbeat_seconds = 5
    backend = ScriptedBackend(handlers)
    orch = Orchestrator(cfg, backend=backend)
    fake = FakeTelegram()
    cfg.secrets.telegram_enabled = True
    cfg.secrets.telegram_bot_token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    cfg.secrets.telegram_chat_id = "1001"
    orch.notifier.secrets = cfg.secrets
    orch.notifier.client_factory = fake.factory()
    return orch, backend, fake


def _titles(fake: FakeTelegram) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m["text"]).splitlines()[0] for m in fake.messages]


# ---------------------------------------------------------------------- full
@pytest.mark.slow
def test_full_flask_pipeline_security_fix_and_untrusted_validation(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    user_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True).stdout.strip()
    orch, backend, fake = _orchestrator(tmp_path, project, _flask_handlers({}))
    summary = asyncio.run(orch.run(max_tasks=5))
    state, pid = orch.state, orch.pid

    assert summary["status"] == "COMPLETED", summary
    tasks = state.list_tasks(pid)
    assert len(tasks) == 1 and tasks[0]["status"] == "COMPLETED", tasks[0].get("blocked_reason")
    task = tasks[0]
    assert task["retry_count"] == 2  # one security repair + one validation repair

    stages = [h["to_stage"] for h in state.task_history(task["id"])]
    assert stages.count("BUG_FIXING") == 2 and stages[-1] == "COMPLETED"
    assert stages.index("SECURITY_TESTING") < stages.index("BUG_FIXING")  # scanners caught what review missed

    reqs = {r["title"]: r for r in state.list_requirements(pid)}
    assert reqs["Inventory statistics endpoint"]["status"] == "COMPLETED"
    assert reqs["Inventory statistics endpoint"]["validation_evidence"]["tasks"][0]["commit"]
    assert reqs["Customer management"]["status"] == "PROPOSED"  # inferred -> needs human approval
    assert any(a["kind"] == "REQUIREMENT" and a["status"] == "PENDING" for a in state.list_approvals(pid))

    findings = state.list_security_findings(pid)
    sqli = [f for f in findings if "sql-injection" in (f["rule_id"] or "")]
    assert sqli and all(f["status"] == "FIXED" for f in sqli)
    acceptance = [r for r in state.list_test_runs(pid, task["id"]) if r["test_type"] == "acceptance"]
    assert [r["status"] for r in reversed(acceptance)] == ["FAIL", "PASS"]  # the false PASS claim was caught
    e2e = [r for r in state.list_test_runs(pid, task["id"]) if r["test_type"] == "e2e"]
    assert e2e and all(r["status"] == "PASS" for r in e2e) and e2e[0]["passed"] == 3

    checkpoints = state.list_checkpoints(pid)
    assert len(checkpoints) == 1
    workspace = orch.workspace
    log = subprocess.run(["git", "log", "-1", "--format=%s%n%b"], cwd=workspace, capture_output=True, text=True).stdout
    assert log.startswith("feat: add item statistics endpoint") and f"Autonomous-Task: {task['id']}" in log
    assert subprocess.run(["git", "branch", "--show-current"], cwd=workspace, capture_output=True, text=True).stdout.strip() == "autonomous-development"
    assert subprocess.run(["git", "rev-parse", "main"], cwd=project, capture_output=True, text=True).stdout.strip() == user_head  # main untouched
    assert SAFE_LINE in (workspace / "inventory" / "api.py").read_text(encoding="utf-8")

    issues = state.issues_for_task(task["id"])
    assert any(i["category"] == "security" and i["status"] == "VERIFIED" for i in issues)
    events = [e["event_type"] for e in state.events_after(0, 5000, pid)]
    for expected in ("SECURITY_FINDING", "BUG_FIXED", "VALIDATION_COMPLETED", "CHECKPOINT_CREATED", "TASK_COMPLETED", "AGENT_TOOL_STARTED"):
        assert expected in events, expected
    titles = _titles(fake)
    for expected in ("Autonomous Development Started", "Security Findings", "Bug fix applied", "Product Validation Failed", "Product Validation Passed", "Task Completed"):
        assert any(expected in t for t in titles), (expected, titles)
    assert [a for a in state.query("SELECT * FROM agent_runs WHERE project_id = ?", (pid,)) if a["backend"] != "scripted"] == []


@pytest.mark.slow
def test_full_pyside_pipeline(tmp_path):
    project = copy_sample("pyside-notes", tmp_path)
    criteria = ["The main window shows a label with the number of notes", "The label updates after a note is saved or deleted"]

    def developer(call):
        text = call.read_file("notes_app/window.py")
        text = text.replace('        self.error = QLabel("")', '        self.count_label = QLabel("0 notes")\n        self.count_label.setObjectName("count")\n        self.error = QLabel("")')
        text = text.replace("        left.addWidget(self.list)\n", "        left.addWidget(self.list)\n        left.addWidget(self.count_label)\n")
        text = text.replace("        self.list.blockSignals(False)\n", "        self.list.blockSignals(False)\n        self.count_label.setText(f\"{len(self.store.notes)} notes\")\n")
        call.write_file("notes_app/window.py", text)
        return {"status": "PASS", "summary": "count label added", "modified_files": ["notes_app/window.py"]}

    qa_test = ("import pytest\nfrom PySide6.QtCore import Qt\npytestmark = pytest.mark.qt\n\n\ndef test_count_label(qtbot, window):\n"
               "    assert window.count_label.text() == '0 notes'\n    qtbot.keyClicks(window.title, 'One')\n"
               "    qtbot.mouseClick(window.save_button, Qt.MouseButton.LeftButton)\n    assert window.count_label.text() == '1 notes'\n")
    acceptance = ("import pytest\nfrom PySide6.QtCore import Qt\n\n\ndef test_label_shows_count(qtbot, window):\n    assert window.count_label.isVisible() and window.count_label.text() == '0 notes'\n\n\n"
                  "def test_label_updates(qtbot, window):\n    qtbot.keyClicks(window.title, 'Note')\n    qtbot.mouseClick(window.save_button, Qt.MouseButton.LeftButton)\n"
                  "    assert window.count_label.text() == '1 notes'\n    qtbot.mouseClick(window.delete_button, Qt.MouseButton.LeftButton)\n    assert window.count_label.text() == '0 notes'\n")

    def qa(call):
        call.write_file("tests/test_count_qt.py", qa_test)
        return {"status": "PASS", "summary": "qt tests", "project_type_detected": "pyside", "test_files_written": ["tests/test_count_qt.py"]}

    def validator(call):
        path = f"tests/acceptance/test_task_{call.ctx.request.task_id}_count.py"
        call.write_file(path, acceptance)
        return {"status": "PASS", "summary": "validated", "recommend_completion": True,
                "criteria_results": [{"criterion": criteria[0], "verdict": "PASS", "evidence_test_ids": [f"{path}::test_label_shows_count"]},
                                     {"criterion": criteria[1], "verdict": "PASS", "evidence_test_ids": [f"{path}::test_label_updates"]}]}

    handlers = {
        "product_manager": lambda call: {"status": "PASS", "summary": "ok", "requirements": [{"title": "Note counter", "description": "Show note count", "acceptance_criteria": criteria, "source": "documented"}],
                                         "tasks": [{"title": "Show note count", "description": "Add a note count label", "requirement_title": "Note counter", "acceptance_criteria": criteria}]},
        "architect": lambda call: {"status": "PASS", "summary": "d", "approach": "QLabel updated in refresh()", "files_to_modify": ["notes_app/window.py"]},
        "developer": developer,
        "code_reviewer": lambda call: {"status": "PASS", "summary": "ok", "acceptance_criteria_met": [{"criterion": c, "met": True} for c in criteria]},
        "qa_engineer": qa,
        "security_auditor": lambda call: {"status": "PASS", "summary": "no issues"},
        "product_validator": validator,
    }
    bare = tmp_path / "github-stand-in.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=project, check=True, capture_output=True)
    orch, _, fake = _orchestrator(tmp_path, project, handlers, pyside={"startup_seconds": 4}, git_push_enabled=True, stop_after="task")
    summary = asyncio.run(orch.run(max_tasks=3))
    task = orch.state.list_tasks(orch.pid)[0]
    assert task["status"] == "COMPLETED", task.get("blocked_reason")
    assert summary["status"] == "STOPPED" and "stop_after=task" in summary["reason"]  # stops after each completed task
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=orch.workspace, capture_output=True, text=True).stdout.strip()
    pushed = subprocess.run(["git", "rev-parse", "autonomous-development"], cwd=bare, capture_output=True, text=True).stdout.strip()
    assert pushed == head  # the checkpoint commit reached the remote
    body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=orch.workspace, capture_output=True, text=True).stdout
    assert "Validated acceptance criteria:" in body
    runs = {r["test_type"]: r for r in reversed(orch.state.list_test_runs(orch.pid, task["id"]))}
    assert runs["qt"]["status"] == "PASS" and runs["qt"]["passed"] == 8
    assert runs["gui"]["status"] == "BLOCKED"
    assert "e2e" not in runs  # Playwright is never forced on a desktop application
    deployments = orch.state.list_deployments(orch.pid)
    assert any(d["method"] == "desktop" and d["status"] == "STOPPED" for d in deployments)


# -------------------------------------------------------------- resume/recovery
def _dev_handlers(stop_after: str | None, state_ref: dict):
    def developer(call):
        call.write_file("inventory/stats.py", "def total(items):\n    return sum(i['quantity'] for i in items)\n")
        if stop_after == "developer":
            state_ref["state"].set_flag(f"stop:{state_ref['pid']}", "1", "test")
        return {"status": "PASS", "summary": "done", "modified_files": ["inventory/stats.py"]}

    def reviewer(call):
        if stop_after == "reviewer":
            state_ref["state"].set_flag(f"stop:{state_ref['pid']}", "1", "test")
        return _reviewer_pass(call)

    return {"product_manager": lambda c: _pm_output(False), "architect": _architect, "developer": developer, "code_reviewer": reviewer}


def test_stop_then_resume_from_persisted_stage(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    orch, _, _ = _orchestrator(tmp_path, project, _dev_handlers("developer", ref))
    ref.update(state=orch.state, pid=orch.pid)
    first = asyncio.run(orch.run())
    task = orch.state.list_tasks(orch.pid)[0]
    assert first["status"] == "STOPPED" and task["status"] == "IN_PROGRESS" and task["stage"] == "CODE_REVIEW"

    # A brand-new controller instance (as after a restart) resumes from the database.
    orch2, backend2, _ = _orchestrator(tmp_path, project, _dev_handlers("reviewer", ref))
    ref.update(state=orch2.state, pid=orch2.pid)
    second = asyncio.run(orch2.run())
    task = orch2.state.get_task(task["id"])
    assert second["status"] == "STOPPED" and task["stage"] == "QA_TESTING"
    assert [c[0] for c in backend2.calls] == ["code_reviewer"]  # no repeated planning/design/development
    assert len(orch2.state.list_tasks(orch2.pid)) == 1  # no duplicate tasks


def _dead_pid() -> int:
    pid = 999_996
    while psutil.pid_exists(pid):
        pid -= 4
    return pid


def test_crash_recovery_takes_over_lock_and_reruns_interrupted_stage(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    orch, _, _ = _orchestrator(tmp_path, project, _dev_handlers("developer", ref))
    ref.update(state=orch.state, pid=orch.pid)
    asyncio.run(orch.run())  # task now IN_PROGRESS at CODE_REVIEW
    state, pid = orch.state, orch.pid
    task = state.list_tasks(pid)[0]
    # Simulate a crash in the middle of CODE_REVIEW: lock held by a dead process, open stage + agent run.
    state.force_take_lock(f"orchestrator:{pid}", "crashed-owner", _dead_pid(), 3600)
    state.start_stage_run(pid, task["id"], "CODE_REVIEW")
    state.start_agent_run(pid, task["id"], "code_reviewer", "CODE_REVIEW", "scripted", "", 1, None)
    state.acquire_task_lease(task["id"], "crashed-owner", -1)

    orch2, backend2, fake = _orchestrator(tmp_path, project, _dev_handlers("reviewer", ref))
    ref.update(state=orch2.state, pid=orch2.pid)
    asyncio.run(orch2.run())
    recovery = [e for e in orch2.state.events_after(0, 5000, pid) if e["event_type"] == "SYSTEM_RECOVERY"]
    assert recovery and "interrupted" in recovery[-1]["summary"]
    assert orch2.state.query_one("SELECT status, error_message FROM agent_runs WHERE agent_name = 'code_reviewer' ORDER BY id LIMIT 1")["status"] == "ERROR"
    assert orch2.state.get_task(task["id"])["stage"] == "QA_TESTING"
    assert any("Recovered after interruption" in t for t in _titles(fake))


def test_second_live_controller_is_refused(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    orch, _, _ = _orchestrator(tmp_path, project, {})
    import os

    orch.state.force_take_lock(f"orchestrator:{orch.pid}", "other-live-owner", os.getpid(), 3600)
    from orchestrator import StopRun

    with pytest.raises(StopRun) as info:
        asyncio.run(orch.run())
    assert "already running" in info.value.reason


# ------------------------------------------------------------------ retries
def test_retry_limit_blocks_task_and_other_tasks_continue(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)

    def pm(call):
        out = _pm_output(False)
        out["tasks"].append({"title": "Impossible task", "description": "x", "requirement_title": "Inventory statistics endpoint", "priority": 2})
        return out

    def architect(call):
        if "Impossible task" in call.prompt:
            return {"status": "FAIL", "summary": "contradictory requirements"}
        return _architect(call)

    def developer(call):
        call.write_file("inventory/stats.py", "VALUE = 1\n")
        return {"status": "PASS", "summary": "done"}

    def reviewer(call):
        return {"status": "FAIL", "summary": "wrong", "findings": [{"title": "Logic error", "severity": "HIGH", "file_path": "inventory/stats.py", "line_number": 1, "explanation": "always 1", "recommendation": "compute it"}]}

    def fixer(call):
        return {"status": "PASS", "summary": "tried", "fixes": [{"issue_id": i, "root_cause": "?", "fix_description": "attempt"} for i in _issue_ids(call.prompt)]}

    handlers = {"product_manager": pm, "architect": architect, "developer": developer, "code_reviewer": reviewer, "bug_fixer": fixer}
    orch, backend, fake = _orchestrator(tmp_path, project, handlers)
    summary = asyncio.run(orch.run())
    tasks = {t["title"]: t for t in orch.state.list_tasks(orch.pid)}
    stats = tasks["Add item statistics endpoint"]
    assert stats["status"] == "BLOCKED" and "maximum repair attempts (3)" in stats["blocked_reason"]
    assert stats["retry_count"] == 3
    assert [c[0] for c in backend.calls].count("code_reviewer") == 4 and [c[0] for c in backend.calls].count("bug_fixer") == 3
    assert tasks["Impossible task"]["status"] == "BLOCKED"  # the loop continued with the next task
    assert summary["tasks_blocked"] == 2
    assert sum("Task Blocked" in t for t in _titles(fake)) == 2
    assert orch.state.get_requirement(stats["requirement_id"])["status"] != "COMPLETED"


def test_illegal_transition_is_rejected(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    orch, _, _ = _orchestrator(tmp_path, project, {})
    tid, _ = orch.state.create_task(orch.pid, "t")
    with pytest.raises(RuntimeError):
        orch._transition(orch.state.get_task(tid), Stage.DEVELOPMENT, StageOutcome(Stage.GIT_CHECKPOINT))  # cannot skip testing


def test_inferred_requirement_waits_for_approval(tmp_path):
    state = StateManager(tmp_path / "s.db")
    tm = TaskManager(state, None, "p", 3, auto_approve_inferred=False)
    output = ProductManagerOutput.model_validate({
        "status": "PASS", "summary": "s",
        "requirements": [{"title": "Export", "description": "export notes", "acceptance_criteria": ["file written"], "source": "inferred"}],
        "tasks": [{"title": "Implement export", "description": "d", "requirement_title": "Export"}],
    })
    mapping = tm.register_requirements(output)
    task_ids = tm.register_tasks(output, mapping)
    assert state.get_task(task_ids[0])["status"] == "ON_HOLD" and state.runnable_tasks("p", "me") == []
    ok, _ = tm.apply_command("APPROVE_REQUIREMENT", {"code": "REQ-001"}, "tester")
    assert ok and state.get_task(task_ids[0])["status"] == "PENDING"
    tm.register_tasks(output, mapping)  # running planning again does not duplicate
    assert len(state.list_tasks("p")) == 1


# ---------------------------------------------------------------- new mode
def test_new_project_mode_discovery_spec_architecture_and_planning(tmp_path):
    project = tmp_path / "brand-new-app"
    project.mkdir()
    calls = {"discovery": 0}

    def pm(call):
        if call.stage == "PROJECT_DISCOVERY":
            calls["discovery"] += 1
            if calls["discovery"] == 1:
                return {"status": "BLOCKED", "summary": "missing info", "questions": ["Web or desktop UI?", "Which data must be stored?"]}
            return {"status": "PASS", "summary": "enough information"}
        call.write_file("docs/PRD.md", "# PRD\n")
        return {"status": "PASS", "summary": "spec", "documents_written": ["docs/PRD.md"],
                "requirements": [{"title": "Task list", "description": "Users manage to-do tasks", "acceptance_criteria": ["Tasks can be added"], "source": "documented"}],
                "tasks": [{"title": "Implement task list", "description": "CRUD for tasks", "requirement_title": "Task list"}]}

    def architect(call):
        call.write_file("docs/architecture.md", "# Architecture\n")
        return {"status": "PASS", "summary": "stack proposed", "technology_stack": ["Flask", "SQLite"], "requires_human_approval": True, "approval_reason": "no technology preference given"}

    handlers = {"product_manager": pm, "architect": architect}
    orch, _, _ = _orchestrator(tmp_path, project, handlers, project_mode="new")
    first = asyncio.run(orch.run(max_tasks=0))
    brief = project / "requirements" / "brief.md"
    assert first["status"] == "AWAITING_INPUT" and brief.exists()
    assert subprocess.run(["git", "branch", "--show-current"], cwd=project, capture_output=True, text=True).stdout.strip() == "autonomous-development"

    brief.write_text("# Brief\n\nA to-do list web application for small teams.\n", encoding="utf-8")
    second = asyncio.run(_orchestrator(tmp_path, project, handlers, project_mode="new")[0].run(max_tasks=0))
    assert second["status"] == "AWAITING_INPUT" and (project / "requirements" / "open_questions.md").exists()

    third_orch = _orchestrator(tmp_path, project, handlers, project_mode="new")[0]
    third = asyncio.run(third_orch.run(max_tasks=0))
    assert third["status"] == "AWAITING_INPUT" and "needs approval" in third["reason"]
    approval = [a for a in third_orch.state.list_approvals(third_orch.pid) if a["kind"] == "TECH_STACK"][0]
    TaskManager(third_orch.state, None, third_orch.pid, 3, False).apply_command("APPROVE", {"approval_id": approval["id"]}, "human")

    fourth_orch = _orchestrator(tmp_path, project, handlers, project_mode="new")[0]
    asyncio.run(fourth_orch.run(max_tasks=0))
    tasks = {t["title"]: t for t in fourth_orch.state.list_tasks(fourth_orch.pid)}
    assert set(tasks) == {"Initialize project structure", "Implement task list"}
    assert fourth_orch.state.task_dependencies(tasks["Implement task list"]["id"]) == [tasks["Initialize project structure"]["id"]]
    assert subprocess.run(["git", "rev-parse", "--verify", "--quiet", "main"], cwd=project, capture_output=True).returncode != 0  # never committed to main
