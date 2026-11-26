"""Phase 1 tests: configuration, logging redaction, state manager and safe execution."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from config import ConfigError, ProjectConfig, load_config, parse_env_file
from logging_setup import redact, register_secret_values
from safe_exec import CommandPolicy, CommandRejected, SafeExecutor, base_environment


# ----------------------------------------------------------------- config
def test_production_deployment_is_rejected():
    with pytest.raises(Exception) as info:
        ProjectConfig(project_name="x", project_path=".", production_deployment=True)
    assert "production" in str(info.value).lower()


def test_development_branch_cannot_be_main():
    with pytest.raises(Exception):
        ProjectConfig(project_name="x", project_path=".", development_branch="main")


def test_project_id_is_derived():
    cfg = ProjectConfig(project_name="Inventory Management!", project_path=".")
    assert cfg.project_id == "inventory-management"


def test_load_default_config_files():
    cfg = load_config()
    assert cfg.project.max_retries_per_task == 3
    assert cfg.security.block_severity_threshold == "HIGH"
    assert cfg.testing.flask.playwright is True


def test_env_file_parsing_and_no_environ_export(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_BOT_TOKEN="123456:abcdefghijklmnopqrstuvwxyzABCDEF"\n# comment\nTELEGRAM_CHAT_ID=42\n', encoding="utf-8")
    values = parse_env_file(env)
    assert values["TELEGRAM_CHAT_ID"] == "42"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    project_cfg = tmp_path / "project.json"
    project_cfg.write_text(json.dumps({"project_name": "t", "project_path": str(tmp_path)}), encoding="utf-8")
    cfg = load_config(project_cfg, env_path=env)
    assert cfg.secrets.telegram_bot_token.startswith("123456:")
    import os

    assert "TELEGRAM_BOT_TOKEN" not in os.environ  # secrets stay in memory only


# ---------------------------------------------------------------- logging
def test_redaction_of_known_and_pattern_secrets():
    register_secret_values(["supersecretvalue123"])
    text = "token=abcd1234efgh sk-ant-api03-ABCDEFGHIJKLMNOP and supersecretvalue123 Bearer abcdefghijklmnopqrstu"
    cleaned = redact(text)
    assert "supersecretvalue123" not in cleaned
    assert "sk-ant-api03" not in cleaned
    assert "abcd1234efgh" not in cleaned
    assert "abcdefghijklmnopqrstu" not in cleaned


# ---------------------------------------------------------- state manager
def test_schema_migrations_are_idempotent(state):
    assert state.schema_version() >= 1
    assert state.migrate() == state.schema_version()


def test_requirement_dedup_and_completion_requires_evidence(state):
    rid, created = state.add_requirement("p", "Customer management", "Manage customers", acceptance_criteria=["AC1"])
    assert created
    rid2, created2 = state.add_requirement("p", "customer management", "Manage customers")
    assert rid2 == rid and not created2
    with pytest.raises(ValueError):
        state.update_requirement(rid, status="COMPLETED")
    state.update_requirement(rid, status="COMPLETED", validation_evidence={"tests": ["t::a"]})
    assert state.get_requirement(rid)["status"] == "COMPLETED"


def test_task_transition_is_recorded_atomically(state):
    tid, _ = state.create_task("p", "Build feature")
    state.transition_task(tid, to_stage="DEVELOPMENT", to_status="IN_PROGRESS", reason="start")
    task = state.get_task(tid)
    assert task["stage"] == "DEVELOPMENT" and task["status"] == "IN_PROGRESS"
    history = state.task_history(tid)
    assert [h["to_stage"] for h in history] == ["ARCHITECTURE_DESIGN", "DEVELOPMENT"]


def test_completed_task_cannot_be_reopened(state):
    tid, _ = state.create_task("p", "Done task")
    state.transition_task(tid, to_status="COMPLETED", to_stage="COMPLETED")
    with pytest.raises(ValueError):
        state.transition_task(tid, to_status="PENDING")


def test_task_leases_prevent_duplicate_execution(state):
    tid, _ = state.create_task("p", "Leased task")
    assert state.acquire_task_lease(tid, "owner-a", 60)
    assert not state.acquire_task_lease(tid, "owner-b", 60)
    state.release_task_lease(tid, "owner-a")
    assert state.acquire_task_lease(tid, "owner-b", 60)


def test_expired_lease_can_be_taken_over(state):
    tid, _ = state.create_task("p", "Expiring")
    assert state.acquire_task_lease(tid, "owner-a", -1)  # already expired
    assert state.acquire_task_lease(tid, "owner-b", 60)


def test_dependencies_gate_runnable_tasks(state):
    a, _ = state.create_task("p", "A", priority=2)
    b, _ = state.create_task("p", "B", priority=1)
    state.add_dependency(b, a)
    runnable = [t["id"] for t in state.runnable_tasks("p", "me")]
    assert runnable == [a]
    state.transition_task(a, to_status="COMPLETED", to_stage="COMPLETED")
    assert [t["id"] for t in state.runnable_tasks("p", "me")] == [b]


def test_named_lock(state):
    ok, _ = state.acquire_lock("orchestrator:p", "one", 1, 60)
    assert ok
    ok2, holder = state.acquire_lock("orchestrator:p", "two", 2, 60)
    assert not ok2 and holder["owner"] == "one"
    state.release_lock("orchestrator:p", "one")
    ok3, _ = state.acquire_lock("orchestrator:p", "two", 2, 60)
    assert ok3


def test_notification_dedupe(state):
    first = state.enqueue_notification("p", "TASK_STARTED", "hi", "k1", 600)
    second = state.enqueue_notification("p", "TASK_STARTED", "hi", "k1", 600)
    assert first is not None and second is None


def test_file_reservations_conflict(state):
    ok, _ = state.reserve_files("p", 1, ["src/a.py"])
    assert ok
    ok2, conflicts = state.reserve_files("p", 2, ["src/a.py", "src/b.py"])
    assert not ok2 and conflicts == ["src/a.py"]


# ----------------------------------------------------------- safe exec
def test_safe_exec_runs_allowed_python(executor, tmp_path):
    result = executor.run([sys.executable, "--version"], tmp_path, timeout=30)
    assert result.ok and "Python" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "origin", "main"],
        ["git", "reset", "--hard"],
        ["git", "clean", "-fd"],
        ["git", "branch", "-D", "feature"],
        ["git", "commit", "--amend", "-m", "x"],
        ["git", "checkout", "--", "."],
        ["git", "rebase", "main"],
        ["cmd", "/c", "dir"],
        ["powershell", "-Command", "Get-ChildItem"],
        ["bash", "-c", "ls"],
        ["rm", "-rf", "/"],
    ],
)
def test_destructive_or_unknown_commands_are_rejected(executor, tmp_path, argv):
    with pytest.raises(CommandRejected):
        executor.run(argv, tmp_path)


def test_inline_python_is_rejected(executor, tmp_path):
    with pytest.raises(CommandRejected):
        executor.run([sys.executable, "-c", "print(1)"], tmp_path)


def test_unlisted_python_module_rejected(executor, tmp_path):
    with pytest.raises(CommandRejected):
        executor.run([sys.executable, "-m", "http.server"], tmp_path)


def test_pip_install_rejected(executor, tmp_path):
    with pytest.raises(CommandRejected):
        executor.run([sys.executable, "-m", "pip", "install", "requests"], tmp_path)


def test_cwd_outside_roots_rejected(executor, tmp_path):
    with pytest.raises(CommandRejected):
        executor.run([sys.executable, "--version"], tmp_path.parent)


def test_commit_with_two_messages_allowed(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path])
    assert policy.validate(["git", "commit", "-m", "feat: x", "-m", "Autonomous-Task: 1"], tmp_path) == "git"


def test_rejections_are_audited(executor, state, tmp_path):
    with pytest.raises(CommandRejected):
        executor.run(["git", "push"], tmp_path)
    audit = state.recent_audit(5)
    assert audit[0]["action"] == "command_rejected" and audit[0]["allowed"] == 0


def test_environment_is_scrubbed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    env = base_environment({"APP_MODE": "staging"})
    assert "TELEGRAM_BOT_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["APP_MODE"] == "staging"


def test_timeout_kills_process(tmp_path, state):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    policy = CommandPolicy(allowed_roots=[tmp_path], python_executables=[sys.executable], app_scripts=[str(script)])
    ex = SafeExecutor(policy, state=state)
    start = time.monotonic()
    result = ex.run([sys.executable, str(script)], tmp_path, timeout=2)
    assert result.timed_out and not result.ok
    assert time.monotonic() - start < 20
