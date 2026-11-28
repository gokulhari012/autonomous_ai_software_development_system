"""Project creation, settings, brief, questions, delete and troubleshooting endpoints (through the real controller CLI)."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DASHBOARD = Path(__file__).resolve().parents[1]
CONTROLLER = DASHBOARD.parent / "autonomous-dev"
sys.path[:0] = [str(DASHBOARD), str(CONTROLLER)]

from backend.app import create_app  # noqa: E402
from backend.auth import hash_password  # noqa: E402
from backend.settings import Settings  # noqa: E402
from state_manager import StateManager  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "state.db"
    StateManager(db_path).close()
    settings = Settings(controller_dir=CONTROLLER, db_path=db_path, username="admin", password_hash=hash_password(PASSWORD),
                        secret_key="test-secret-key-for-dashboard-tests-0123456789", allowed_project_roots=[tmp_path])
    client = TestClient(create_app(settings))
    csrf = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).json()["csrf"]
    name = f"Admin Test {uuid.uuid4().hex[:6]}"
    slug = name.lower().replace(" ", "-")
    yield client, {"X-CSRF-Token": csrf}, tmp_path, db_path, name, slug
    leftover = CONTROLLER / "config" / "projects" / f"{slug}.json"
    if leftover.exists():
        leftover.unlink()


def test_create_new_project_with_brief_and_settings(env):
    client, headers, tmp_path, db_path, name, slug = env
    brief = "# Calculator\n\nA Flask calculator web app with history.\n"
    response = client.post("/api/control/create_project", headers=headers, json={
        "name": name, "path": str(tmp_path / "calc"), "mode": "new", "brief": brief,
        "settings": {"stop_after": "manual", "auto_resume_on_limit": True, "git_push_enabled": True,
                     "git_remote_url": "https://github.com/example/calc.git", "application_types": ["flask"]},
    })
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == slug
    assert (tmp_path / "calc" / "requirements" / "brief.md").read_text(encoding="utf-8") == brief
    config = json.loads((CONTROLLER / "config" / "projects" / f"{slug}.json").read_text(encoding="utf-8"))
    assert config["stop_after"] == "manual" and config["git_push_enabled"] is True and config["application_types"] == ["flask"]
    assert config["production_deployment"] is False

    settings = client.get(f"/api/projects/{slug}/settings").json()
    assert settings["project"]["auto_resume_on_limit"] is True and settings["read_only"]["production_deployment"] is False
    assert client.get(f"/api/projects/{slug}/brief").json()["brief"] == brief
    assert any(p["id"] == slug for p in client.get("/api/projects").json())


def test_settings_whitelist_validation_and_rollback(env):
    client, headers, tmp_path, db_path, name, slug = env
    client.post("/api/control/create_project", headers=headers, json={"name": name, "path": str(tmp_path / "app"), "mode": "new"})
    config_file = CONTROLLER / "config" / "projects" / f"{slug}.json"
    before = config_file.read_text(encoding="utf-8")

    for bad in ({"production_deployment": True}, {"development_branch": "main"}, {"max_parallel_tasks": 99}, {"stop_after": "sometimes"}):
        r = client.put(f"/api/projects/{slug}/settings", headers=headers, json={"project": bad})
        assert r.status_code == 400, bad
    # passes the whitelist but the controller rejects the URL -> rolled back
    r = client.put(f"/api/projects/{slug}/settings", headers=headers, json={"project": {"git_remote_url": "file:///etc/passwd"}})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert config_file.read_text(encoding="utf-8") == before

    ok = client.put(f"/api/projects/{slug}/settings", headers=headers, json={"project": {"max_tasks_per_run": 7, "flask.staging_port": 5099}, "workflow": {}})
    assert ok.status_code == 200 and ok.json()["project"]["max_tasks_per_run"] == 7 and ok.json()["project"]["flask.staging_port"] == 5099
    assert client.put(f"/api/projects/{slug}/settings", json={"project": {"max_tasks_per_run": 8}}).status_code == 403  # CSRF


def test_agent_model_and_effort_assignment(env):
    client, headers, tmp_path, db_path, name, slug = env
    client.post("/api/control/create_project", headers=headers, json={"name": name, "path": str(tmp_path / "m"), "mode": "new"})
    url = f"/api/projects/{slug}/settings"
    workflow_file = CONTROLLER / "config" / "workflow.json"
    original = workflow_file.read_text(encoding="utf-8")  # shared by all projects, so restore it afterwards
    try:
        options = client.get(url).json()["options"]
        assert "developer" in options["agents"] and any(m["id"] == "claude-sonnet-5-5" for m in options["models"])

        r = client.put(url, headers=headers, json={"workflow": {"agents.developer.model": "claude-sonnet-5-5", "agents.developer.effort": "max"}})
        assert r.status_code == 200, r.text
        assert r.json()["workflow"]["agents.developer.model"] == "claude-sonnet-5-5"
        developer = json.loads(workflow_file.read_text(encoding="utf-8"))["agents"]["developer"]
        assert developer["model"] == "claude-sonnet-5-5" and developer["effort"] == "max" and developer["max_turns"] == 80

        # "Default" removes the override and leaves the agent's other limits alone
        r = client.put(url, headers=headers, json={"workflow": {"agents.developer.model": "", "agents.developer.effort": ""}})
        assert r.status_code == 200 and r.json()["workflow"]["agents.developer.effort"] == "", r.text
        assert json.loads(workflow_file.read_text(encoding="utf-8"))["agents"]["developer"] == {"max_turns": 80}

        for bad in ({"agents.developer.effort": "extreme"}, {"agents.developer.model": "gpt-x"}, {"agents.developer.max_turns": 5}, {"agents.nobody.model": "claude-opus-5-5"}):
            assert client.put(url, headers=headers, json={"workflow": bad}).status_code == 400, bad
    finally:
        workflow_file.write_text(original, encoding="utf-8")


def test_agent_team_and_automatic_model_settings(env):
    client, headers, tmp_path, db_path, name, slug = env
    client.post("/api/control/create_project", headers=headers, json={"name": name, "path": str(tmp_path / "team"), "mode": "new"})
    url = f"/api/projects/{slug}/settings"
    workflow_file = CONTROLLER / "config" / "workflow.json"
    original = workflow_file.read_text(encoding="utf-8")
    try:
        data = client.get(url).json()
        assert data["project"]["agent_team.mode"] == "full" and "architect" in data["project"]["agent_team.agents"]
        roles = {r["name"]: r for r in data["options"]["agent_roles"]}
        assert roles["code_reviewer"]["required"] and not roles["qa_engineer"]["required"]
        assert data["workflow"]["auto_model_selection"] is False and data["workflow"]["escalation_max_model"] == "claude-opus-5-5"

        r = client.put(url, headers=headers, json={"project": {"agent_team.mode": "custom", "agent_team.agents": ["qa_engineer", "architect"]},
                                                    "workflow": {"auto_model_selection": True, "auto_escalation": True, "escalation_max_model": "claude-fable-5-2"}})
        assert r.status_code == 200, r.text
        config = json.loads((CONTROLLER / "config" / "projects" / f"{slug}.json").read_text(encoding="utf-8"))
        assert config["agent_team"] == {"mode": "custom", "agents": ["architect", "qa_engineer"]}
        assert json.loads(workflow_file.read_text(encoding="utf-8"))["escalation_max_model"] == "claude-fable-5-2"

        for bad in ({"project": {"agent_team.agents": ["developer"]}}, {"project": {"agent_team.mode": "some"}},
                    {"workflow": {"escalation_max_model": "claude-haiku-5-5"}}, {"workflow": {"auto_escalation": "yes"}}):
            assert client.put(url, headers=headers, json=bad).status_code == 400, bad
    finally:
        workflow_file.write_text(original, encoding="utf-8")


def test_existing_project_description_becomes_notes(env):
    client, headers, tmp_path, db_path, name, slug = env
    existing = tmp_path / "existing"
    existing.mkdir()
    r = client.post("/api/control/create_project", headers=headers, json={"name": name, "path": str(existing), "mode": "existing", "brief": "Add a customers page."})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/projects/{slug}/settings").json()["project"]["project_notes"] == "Add a customers page."
    assert not (existing / "requirements").exists()  # the user's folder is not written to


def test_questions_answer_and_delete(env):
    client, headers, tmp_path, db_path, name, slug = env
    folder = tmp_path / "q"
    folder.mkdir()
    (folder / "keep.txt").write_text("user file\n", encoding="utf-8")
    client.post("/api/control/create_project", headers=headers, json={"name": name, "path": str(folder), "mode": "new"})
    state = StateManager(db_path)
    approval_id, _ = state.request_approval(slug, "QUESTIONS", "PROJECT_DISCOVERY:1", {"stage": "PROJECT_DISCOVERY", "questions": ["Web or desktop?"]})
    assert client.get(f"/api/questions?project={slug}").json()[0]["details"]["questions"] == ["Web or desktop?"]
    overview = client.get(f"/api/overview?project={slug}").json()
    assert any("Questions" in a["title"] for a in overview["attention"])

    answer = "Web app with Flask.\nKeep it simple."
    r = client.post("/api/control/answer_questions", headers=headers, json={"project": slug, "approval_id": approval_id, "note": answer})
    assert r.status_code == 200 and r.json()["ok"], r.text
    row = sqlite3.connect(db_path).execute("SELECT status, decision_note FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    assert row[0] == "APPROVED" and row[1].strip() == answer

    r = client.post("/api/control/delete_project", headers=headers, json={"project": slug})
    assert r.status_code == 200, r.text
    assert not (CONTROLLER / "config" / "projects" / f"{slug}.json").exists()
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "user file\n"  # project folder and files kept
    assert all(p["id"] != slug for p in client.get("/api/projects").json())


def test_troubleshooting_endpoints(env):
    client, headers, tmp_path, db_path, name, slug = env
    client.post("/api/control/create_project", headers=headers, json={"name": name, "path": str(tmp_path / "t"), "mode": "new"})
    r = client.post("/api/control/validate_config", headers=headers, json={"project": slug})
    assert r.status_code == 200 and r.json()["ok"] and "valid" in r.json()["output"]
    logs = client.get("/api/logs", params={"project": slug, "lines": 50}).json()
    assert "controller" in logs
    git = client.post("/api/control/git_check", headers=headers, json={"project": slug}).json()
    assert git["ok"] is False  # folder not created yet -> clear message instead of a crash
