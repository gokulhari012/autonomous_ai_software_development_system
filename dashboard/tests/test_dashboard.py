"""Dashboard tests against a state database produced by a real orchestrator run."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DASHBOARD = Path(__file__).resolve().parents[1]
CONTROLLER = DASHBOARD.parent / "autonomous-dev"
sys.path[:0] = [str(DASHBOARD), str(CONTROLLER)]

from backend.app import create_app  # noqa: E402
from backend.auth import hash_password  # noqa: E402
from backend.settings import Settings  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="module")
def orchestrated_db(tmp_path_factory) -> tuple[Path, str]:
    """Run the real orchestrator (scripted agents) until the developer finished, then stop."""
    from agent_runner import ScriptedBackend
    from orchestrator import Orchestrator
    from tests.conftest import copy_sample, make_config

    tmp = tmp_path_factory.mktemp("dash")
    project = copy_sample("flask-inventory", tmp)
    cfg = make_config(tmp, project, project_name="Dashboard Test", worktree_dir=str(tmp / "wt"))
    cfg.security.semgrep.use_registry = False
    holder: dict = {}

    def developer(call):
        call.write_file("inventory/stats.py", "def total():\n    return 0\n")
        holder["state"].set_flag(f"stop:{holder['pid']}", "1", "test")
        return {"status": "PASS", "summary": "added stats module", "modified_files": ["inventory/stats.py"]}

    handlers = {
        "product_manager": lambda c: {"status": "PASS", "summary": "analysis", "requirements": [{"title": "Stats", "description": "stats", "acceptance_criteria": ["works"], "source": "documented"}],
                                       "tasks": [{"title": "Add stats", "description": "d", "requirement_title": "Stats"}]},
        "architect": lambda c: {"status": "PASS", "summary": "design", "approach": "new module", "files_to_create": ["inventory/stats.py"]},
        "developer": developer,
    }
    orch = Orchestrator(cfg, backend=ScriptedBackend(handlers))
    holder.update(state=orch.state, pid=orch.pid)
    asyncio.run(orch.run())
    return Path(cfg.db_path), orch.pid


@pytest.fixture
def client(orchestrated_db, tmp_path):
    db_path, _ = orchestrated_db
    settings = Settings(controller_dir=CONTROLLER, db_path=db_path, username="admin", password_hash=hash_password(PASSWORD),
                        secret_key="test-secret-key-for-dashboard-tests-0123456789", allowed_project_roots=[tmp_path])
    return TestClient(create_app(settings))


def login(client: TestClient) -> str:
    response = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["csrf"]


def test_requires_authentication(client):
    assert client.get("/api/health").status_code == 200
    for path in ("/api/overview", "/api/events", "/api/agents", "/api/tasks", "/api/git", "/api/usage"):
        assert client.get(path).status_code == 401, path


def test_wrong_password_and_rate_limit(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 429


def test_unconfigured_password_disables_login(orchestrated_db):
    settings = Settings(controller_dir=CONTROLLER, db_path=orchestrated_db[0], password_hash=None, secret_key="k" * 40)
    c = TestClient(create_app(settings))
    assert c.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code == 503


def test_views_show_real_orchestrator_activity(client, orchestrated_db):
    _, pid = orchestrated_db
    login(client)
    overview = client.get(f"/api/overview?project={pid}").json()
    assert overview["project"]["id"] == pid and overview["tasks"]["total"] == 1
    assert overview["controller"]["running"] is False and overview["active_agents"] == []  # nothing is shown active when not executing
    events = client.get(f"/api/events?project={pid}&limit=2000").json()
    types = {e["event_type"] for e in events}
    assert {"SYSTEM_STARTED", "AGENT_STARTED", "AGENT_TOOL_STARTED", "AGENT_COMPLETED", "TASK_STAGE_CHANGED", "SYSTEM_STOPPED"} <= types
    filtered = client.get(f"/api/events?project={pid}&agent=developer").json()
    assert filtered and all(e["agent_name"] == "developer" for e in filtered)
    agents = {a["name"]: a for a in client.get(f"/api/agents?project={pid}").json()}
    assert len(agents) == 9 and agents["developer"]["last_run"]["status"] == "PASS" and not agents["developer"]["active"]
    run = client.get(f"/api/agent-runs/{agents['developer']['last_run']['id']}").json()
    assert any(e["event_type"] == "AGENT_TOOL_STARTED" for e in run["events"])
    tasks = client.get(f"/api/tasks?project={pid}").json()
    assert tasks[0]["stage"] == "CODE_REVIEW" and "lease_owner" not in tasks[0]
    detail = client.get(f"/api/tasks/{tasks[0]['id']}").json()
    assert [h["to_stage"] for h in detail["history"]][-1] == "CODE_REVIEW"
    git = client.get(f"/api/git?project={pid}").json()
    assert git["branch"] == "autonomous-development" and any(f["path"].endswith("stats.py") for f in git["modified_files"])
    security = client.get(f"/api/security?project={pid}").json()
    assert {s["tool_name"] for s in security["scans"]} >= {"bandit", "semgrep"}
    tests = client.get(f"/api/tests?project={pid}").json()
    assert "pytest" in tests["latest_by_layer"]  # baseline test run recorded during PROJECT_SCAN
    usage = client.get(f"/api/usage?project={pid}").json()
    assert usage["totals"]["scripted_runs"] == usage["totals"]["agent_runs"] > 0


def test_control_requires_csrf_and_sets_flags(client, orchestrated_db):
    db_path, pid = orchestrated_db
    csrf = login(client)
    assert client.post("/api/control/pause", json={"project": pid}).status_code == 403
    response = client.post("/api/control/pause", json={"project": pid}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT value FROM control_flags WHERE key = ?", (f"pause:{pid}",)).fetchone()[0] == "1"
    client.post("/api/control/resume", json={"project": pid}, headers={"X-CSRF-Token": csrf})
    assert conn.execute("SELECT value FROM control_flags WHERE key = ?", (f"pause:{pid}",)).fetchone()[0] == "0"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'dashboard:admin' AND action = 'pause'").fetchone()[0] >= 1
    conn.close()


def test_control_input_validation(client, orchestrated_db, tmp_path):
    _, pid = orchestrated_db
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf}
    outside = client.post("/api/control/open_project", json={"name": "X", "path": "C:/Windows", "mode": "existing"}, headers=headers)
    assert outside.status_code == 400 and "outside the allowed project roots" in outside.json()["detail"]
    bad_name = client.post("/api/control/new_project", json={"name": "x; rm -rf /", "path": str(tmp_path / "p")}, headers=headers)
    assert bad_name.status_code == 400
    bad_code = client.post("/api/control/approve_requirement", json={"project": pid, "code": "REQ-1; drop"}, headers=headers)
    assert bad_code.status_code in (400, 422)
    unknown = client.post("/api/control/run_shell", json={"project": pid}, headers=headers)
    assert unknown.status_code == 404
    # the project's config lives outside autonomous-dev/config in this test -> the bridge refuses it
    retry = client.post("/api/control/retry_task", json={"project": pid, "task_id": 1}, headers=headers)
    assert retry.status_code == 400 and "autonomous-dev/config" in retry.json()["detail"]


def test_artifact_endpoint_blocks_traversal(client):
    login(client)
    assert client.get("/api/artifact", params={"path": str(CONTROLLER / "config" / "project.json")}).status_code == 404
    assert client.get("/api/artifact", params={"path": str(CONTROLLER / "reports" / ".." / "main.py")}).status_code == 404


def test_websocket_auth_replay_and_validation(client, orchestrated_db):
    _, pid = orchestrated_db
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/events?project={pid}") as ws:  # no session cookie
            ws.receive_json()
    login(client)
    with client.websocket_connect(f"/ws/events?project={pid}&after_id=0") as ws:
        first = ws.receive_json()
        assert first["type"] == "events" and first["events"][0]["id"] >= 1
        last = first["last_id"]
    # reconnect after the last seen id: only newer events are replayed (none yet)
    with client.websocket_connect(f"/ws/events?project={pid}&after_id=0") as ws:
        ws.receive_json()
        ws.send_text('{"type": "subscribe", "project": "%s", "after_id": %d}' % (pid, max(last - 3, 0)))
        replay = ws.receive_json()
        assert replay["type"] == "events" and all(e["id"] > last - 3 for e in replay["events"])
        ws.send_text('{"type": "ping"}')
        assert ws.receive_json()["type"] == "pong"
        for _ in range(3):
            ws.send_text('{"type": "exec", "cmd": "dir"}')
            assert ws.receive_json()["type"] == "error"


def test_websocket_rejects_cross_origin(client, orchestrated_db):
    login(client)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/events", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()


def test_refuses_public_bind(monkeypatch):
    from backend.settings import load_settings

    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError):
        load_settings()
