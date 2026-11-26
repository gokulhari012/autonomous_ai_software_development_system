"""Phases 4-7: real test execution, security scanning, staging deployment, Telegram, reports.

These tests run the real tools (pytest, Playwright/Chromium, pytest-qt, Bandit, Semgrep,
a real Flask staging server, a real PySide launch) against copies of the sample apps.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from config import REPORTS_DIR
from deployment_manager import DeploymentManager
from events import EventBus
from project_inspector import inspect_project
from report_generator import generate_final_report
from safe_exec import CommandPolicy, SafeExecutor
from security_scanner import SecurityScanner
from state_manager import StateManager
from telegram_bot import TelegramCommandBot
from telegram_notifier import TelegramNotifier, format_message
from test_manager import TestManager
from tests.conftest import copy_sample, make_config

VULNERABLE_MODULE = '''"""Deliberately vulnerable code used ONLY to verify the scanners (Phase 8)."""
import sqlite3
import subprocess


def find_items(conn: sqlite3.Connection, name: str):
    return conn.execute(f"SELECT * FROM items WHERE name = '{name}'").fetchall()


def export(path: str):
    subprocess.call("tar czf backup.tgz " + path, shell=True)
'''


def _components(tmp_path: Path, project: Path, **cfg_overrides):
    cfg = make_config(tmp_path, project, project_name=f"t-{tmp_path.name}", **cfg_overrides)
    cfg.security.semgrep.use_registry = False  # deterministic, offline local ruleset
    state = StateManager(cfg.db_path)
    events = EventBus(state, cfg.project_id)
    policy = CommandPolicy(allowed_roots=[tmp_path, REPORTS_DIR], python_executables=[sys.executable])
    profile = inspect_project(project)
    if profile.pyside_entry:
        policy.app_modules.add(profile.pyside_entry)
    executor = SafeExecutor(policy, state)
    return cfg, state, events, executor, profile


# ------------------------------------------------------------ detection
def test_project_detection(tmp_path):
    flask = inspect_project(copy_sample("flask-inventory", tmp_path))
    assert flask.application_types == ["flask"] and flask.has_browser_ui
    assert flask.flask_app == "inventory:create_app()" and "sqlite" in flask.databases
    assert flask.detected_mode == "existing" and flask.health_endpoint
    pyside = inspect_project(copy_sample("pyside-notes", tmp_path))
    assert pyside.application_types == ["pyside"] and pyside.pyside_version == "PySide6"
    assert pyside.pyside_entry == "notes_app.main"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert inspect_project(empty).detected_mode == "new"


# ------------------------------------------------------------- testing
@pytest.mark.slow
def test_flask_layers_pytest_and_playwright(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    cfg, state, events, executor, profile = _components(tmp_path, project, flask={"qa_port": 5186, "staging_port": 5185})
    tm = TestManager(cfg, state, events, executor, profile)
    layers = {l.name: l for l in tm.plan_layers(project, "QA_TESTING", None)}
    assert set(layers) == {"pytest", "e2e"}  # Playwright chosen because the app has a browser UI; no Qt
    assert layers["e2e"].blocked_reason  # no server URL yet -> BLOCKED, never PASS
    unit = tm.run_layer(layers["pytest"], project, None, "QA_TESTING")
    assert unit.status == "PASS" and unit.passed == 14 and unit.failed == 0 and unit.junit_path

    deployer = DeploymentManager(cfg, state, events, executor, profile)
    server = asyncio.run(deployer.deploy_flask(project, None, purpose="qa"))
    assert server.ok, server.error
    try:
        e2e_layer = [l for l in tm.plan_layers(project, "QA_TESTING", server.url) if l.name == "e2e"][0]
        e2e = tm.run_layer(e2e_layer, project, None, "QA_TESTING")
        assert e2e.status == "PASS" and e2e.passed == 3, e2e.summary

        # Break the UI: the label "Name" disappears -> real Playwright failure with artifacts.
        # Flask caches templates outside debug mode, so restart the server after the change.
        deployer.stop(server.deployment_id)
        form = project / "inventory" / "templates" / "item_form.html"
        form.write_text(form.read_text(encoding="utf-8").replace('("name", "Name")', '("name", "Title")'), encoding="utf-8")
        server = asyncio.run(deployer.deploy_flask(project, None, purpose="qa"))
        assert server.ok, server.error
        broken = tm.run_layer(e2e_layer, project, None, "QA_TESTING")
        assert broken.status == "FAIL" and broken.failed >= 1
        assert any(a.endswith(".png") for a in broken.artifacts), broken.artifacts
        assert any(a.endswith(".zip") for a in broken.artifacts)  # Playwright trace
    finally:
        deployer.stop(server.deployment_id)


@pytest.mark.slow
def test_pyside_layers_qt_and_gui_blocked(tmp_path):
    project = copy_sample("pyside-notes", tmp_path)
    cfg, state, events, executor, profile = _components(tmp_path, project)
    tm = TestManager(cfg, state, events, executor, profile)
    layers = {l.name: l for l in tm.plan_layers(project, "QA_TESTING")}
    assert set(layers) == {"pytest", "qt", "gui"}  # no Playwright for a desktop app
    suite = tm.run_suite(list(layers.values()), project, None, "QA_TESTING")
    by_layer = {l.layer: l for l in suite.layers}
    assert by_layer["pytest"].status == "PASS" and by_layer["pytest"].passed == 5
    assert by_layer["qt"].status == "PASS" and by_layer["qt"].passed == 7
    assert by_layer["gui"].status == "BLOCKED" and "not authorised" in by_layer["gui"].summary
    assert suite.status == "PASS"  # the optional GUI layer is BLOCKED, reported honestly, not PASS


@pytest.mark.slow
def test_failing_qt_test_captures_screenshot(tmp_path):
    project = copy_sample("pyside-notes", tmp_path)
    (project / "tests" / "test_broken_qt.py").write_text(
        "import pytest\npytestmark = pytest.mark.qt\n\ndef test_wrong_title(window):\n    assert window.windowTitle() == 'Not Notes'\n", encoding="utf-8")
    cfg, state, events, executor, profile = _components(tmp_path, project)
    tm = TestManager(cfg, state, events, executor, profile)
    qt = [l for l in tm.plan_layers(project, "QA_TESTING") if l.name == "qt"][0]
    result = tm.run_layer(qt, project, None, "QA_TESTING")
    assert result.status == "FAIL" and result.failed == 1
    assert any("qt-screenshots" in a and a.endswith(".png") for a in result.artifacts)


# ------------------------------------------------------------ security
@pytest.mark.slow
def test_scanners_find_injected_vulnerabilities_and_mark_fixed(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    cfg, state, events, executor, profile = _components(tmp_path, project)
    cfg.testing.security.pip_audit = False
    scanner = SecurityScanner(cfg, state, events, executor, profile)
    clean = scanner.run_all(project, None, dependency_scan=False)
    assert {r.tool: r.status for r in clean} == {"bandit": "COMPLETED", "semgrep": "COMPLETED", "pip_audit": "SKIPPED"}
    assert scanner.evaluate_policy(clean, set()).status == "PASS"

    vuln = project / "inventory" / "unsafe_reports.py"
    vuln.write_text(VULNERABLE_MODULE, encoding="utf-8")
    results = scanner.run_all(project, 1, dependency_scan=False)
    bandit = next(r for r in results if r.tool == "bandit")
    semgrep = next(r for r in results if r.tool == "semgrep")
    assert any(f["rule_id"].startswith("B602") for f in bandit.findings)  # subprocess shell=True
    assert any(f["rule_id"].startswith("B608") for f in bandit.findings)  # SQL string building
    rules = {f["rule_id"].split(".")[-1] for f in semgrep.findings}
    assert {"autodev-sql-injection-string-built-query", "autodev-command-injection-shell"} <= rules
    decision = scanner.evaluate_policy(results, {"inventory/unsafe_reports.py"})
    assert decision.status == "FAIL" and decision.blocking

    vuln.unlink()
    rescan = scanner.run_all(project, 1, dependency_scan=False)
    assert scanner.evaluate_policy(rescan, set()).status == "PASS"
    fixed = [f for f in state.list_security_findings(cfg.project_id) if f["status"] == "FIXED"]
    assert len(fixed) >= 3


def test_scanner_error_is_not_a_pass(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    cfg, state, events, executor, profile = _components(tmp_path, project)
    cfg.security.semgrep.local_rules = str(tmp_path / "missing-rules.yml")
    cfg.security.semgrep.use_registry = True
    cfg.security.semgrep.registry_configs = [str(tmp_path / "also-missing.yml")]
    scanner = SecurityScanner(cfg, state, events, executor, profile)
    result = scanner.run_semgrep(project, tmp_path)
    assert result.status == "ERROR"
    decision = scanner.evaluate_policy([result], set())
    assert decision.status == "BLOCKED" and decision.scanner_problems


# ---------------------------------------------------------- deployment
@pytest.mark.slow
def test_flask_staging_healthy_and_broken(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    cfg, state, events, executor, profile = _components(tmp_path, project, flask={"staging_port": 5187, "qa_port": 5188, "startup_timeout_seconds": 20})
    deployer = DeploymentManager(cfg, state, events, executor, profile)
    result = asyncio.run(deployer.deploy_flask(project, None))
    assert result.ok and result.detail["health"]["body"]["database"] == "ok"
    assert "staging.db" in result.detail["staging_db"]  # separate staging database
    deployer.stop(result.deployment_id)
    assert state.query_one("SELECT status FROM deployments WHERE id = ?", (result.deployment_id,))["status"] == "STOPPED"

    (project / "inventory" / "__init__.py").write_text("raise RuntimeError('boom during startup')\n", encoding="utf-8")
    broken = asyncio.run(deployer.deploy_flask(project, None))
    assert not broken.ok and "boom during startup" in broken.detail.get("log_tail", "")


@pytest.mark.slow
def test_pyside_desktop_launch_offscreen(tmp_path):
    project = copy_sample("pyside-notes", tmp_path)
    cfg, state, events, executor, profile = _components(tmp_path, project, pyside={"startup_seconds": 4})
    deployer = DeploymentManager(cfg, state, events, executor, profile)
    result = asyncio.run(deployer.deploy_desktop(project, None))
    assert result.ok, result.error
    assert result.detail["display"].startswith("offscreen")
    assert result.detail["packaged"]["status"] == "NOT_APPLICABLE"


# ------------------------------------------------------------ telegram
class FakeTelegram:
    def __init__(self, fail_first_with_429: bool = False):
        self.messages: list[dict] = []
        self.fail = fail_first_with_429
        self.updates: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            updates, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        body = json.loads(request.content)
        if self.fail:
            self.fail = False
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 0}})
        self.messages.append(body)
        return httpx.Response(200, json={"ok": True, "result": {}})

    def factory(self):
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _telegram_config(tmp_path):
    cfg = make_config(tmp_path, tmp_path)
    cfg.secrets.telegram_enabled = True
    cfg.secrets.telegram_bot_token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    cfg.secrets.telegram_chat_id = "1001"
    cfg.secrets.telegram_authorized_user_ids = ["42"]
    return cfg


def test_notifier_escapes_retries_and_dedupes(tmp_path):
    cfg = _telegram_config(tmp_path)
    state = StateManager(cfg.db_path)
    fake = FakeTelegram(fail_first_with_429=True)
    notifier = TelegramNotifier(cfg.secrets, state, cfg.project_id, "Inventory <Mgmt>", 600, client_factory=fake.factory())
    assert notifier.notify("QA_FAILED", "QA Testing Failed", {"Total Tests": 25, "Passed": 22, "Failed": 3}, ["Invalid email accepted <script>"]) is not None
    assert notifier.notify("QA_FAILED", "QA Testing Failed", {"Total Tests": 25, "Passed": 22, "Failed": 3}, ["Invalid email accepted <script>"]) is None
    sent = asyncio.run(notifier.flush())
    assert sent == 1 and len(fake.messages) == 1
    text = fake.messages[0]["text"]
    assert "&lt;script&gt;" in text and "Inventory &lt;Mgmt&gt;" in text and fake.messages[0]["parse_mode"] == "HTML"
    assert cfg.secrets.telegram_bot_token not in text


def test_notifier_disabled_records_only(tmp_path):
    cfg = make_config(tmp_path, tmp_path)
    state = StateManager(cfg.db_path)
    notifier = TelegramNotifier(cfg.secrets, state, cfg.project_id, "x")
    notifier.notify("TASK_STARTED", "Started")
    assert state.list_notifications(1)[0]["status"] == "DISABLED"


def test_bot_authorization_and_commands(tmp_path):
    cfg = _telegram_config(tmp_path)
    state = StateManager(cfg.db_path)
    fake = FakeTelegram()
    bot = TelegramCommandBot(cfg, state, client_factory=fake.factory())
    fake.updates = [
        {"update_id": 1, "message": {"text": "/pause", "chat": {"id": 1001}, "from": {"id": 999}}},  # unauthorised user
        {"update_id": 2, "message": {"text": "/pause", "chat": {"id": 1001}, "from": {"id": 42}}},
        {"update_id": 3, "message": {"text": "/status", "chat": {"id": 1001}, "from": {"id": 42}}},
    ]

    async def poll():
        async with fake.factory()() as client:
            return await bot.poll_once(client, timeout=0)

    assert asyncio.run(poll()) == 3
    assert state.get_flag(f"pause:{cfg.project_id}") == "1"
    assert len(fake.messages) == 2  # no reply to the unauthorised user
    assert any(a["action"] == "unauthorized_command" for a in state.recent_audit(10))
    assert state.kv_get(f"telegram_offset:{cfg.project_id}") == "4"


def test_format_message_truncates():
    text = format_message("T", {"k": "v" * 5000})
    assert len(text) <= 4000


# -------------------------------------------------------------- reports
def test_final_report_lists_incomplete_work(tmp_path):
    cfg = make_config(tmp_path, tmp_path)
    state = StateManager(cfg.db_path)
    state.upsert_project(cfg.project_id, name="Report test", path=str(tmp_path))
    rid, _ = state.add_requirement(cfg.project_id, "Feature A", "desc", status="APPROVED")
    tid, _ = state.create_task(cfg.project_id, "Implement A", requirement_id=rid)
    state.transition_task(tid, to_status="BLOCKED", reason="max retries", blocked_reason="maximum repair attempts reached")
    md, js, criteria = generate_final_report(cfg, state)
    text = md.read_text(encoding="utf-8")
    assert "NOT COMPLETE" in text and "maximum repair attempts reached" in text
    assert criteria["project_complete"] is False
