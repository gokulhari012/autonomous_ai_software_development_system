"""Git push, usage-limit auto-resume, stop modes and the questions/answers loop."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_runner import UsageLimitReached
from git_manager import GitManager
from safe_exec import CommandPolicy, CommandRejected, SafeExecutor
from task_manager import TaskManager
from tests.conftest import copy_sample
from tests.test_orchestrator import _architect, _orchestrator, _pm_output, _reviewer_pass


def _bare_remote(tmp_path: Path, repo: Path, name: str = "origin") -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", name, str(bare)], cwd=repo, check=True, capture_output=True)
    return bare


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


# ------------------------------------------------------------- push policy
@pytest.mark.parametrize(
    "argv,allowed",
    [
        (["git", "push", "--set-upstream", "origin", "autonomous-development"], True),
        (["git", "push", "origin", "autonomous-development"], True),
        (["git", "push", "origin", "main"], False),
        (["git", "push", "--force", "origin", "autonomous-development"], False),
        (["git", "push", "-f", "origin", "autonomous-development"], False),
        (["git", "push", "origin", "+autonomous-development"], False),
        (["git", "push", "origin", "autonomous-development:main"], False),
        (["git", "push", "--delete", "origin", "autonomous-development"], False),
        (["git", "push", "upstream", "autonomous-development"], False),
        (["git", "push", "origin"], False),
        (["git", "remote", "add", "origin", "https://github.com/me/app.git"], True),
        (["git", "remote", "add", "evil", "https://github.com/me/app.git"], False),
        (["git", "remote", "add", "origin", "file:///etc"], False),
        (["git", "remote", "remove", "origin"], False),
        (["git", "ls-remote", "--heads", "origin"], True),
    ],
)
def test_push_policy(tmp_path, argv, allowed):
    policy = CommandPolicy(allowed_roots=[tmp_path], allow_push=True, push_remote="origin", push_branch="autonomous-development")
    if allowed:
        assert policy.validate(argv, tmp_path) == "git"
    else:
        with pytest.raises(CommandRejected):
            policy.validate(argv, tmp_path)


def test_push_disabled_by_default(tmp_path):
    policy = CommandPolicy(allowed_roots=[tmp_path], push_branch="autonomous-development")
    with pytest.raises(CommandRejected, match="disabled"):
        policy.validate(["git", "push", "origin", "autonomous-development"], tmp_path)


def test_commit_body_and_push_to_remote(tmp_path, state, user_repo):
    bare = _bare_remote(tmp_path, user_repo)
    policy = CommandPolicy(allowed_roots=[tmp_path], python_executables=[sys.executable], allow_push=True, push_branch="autonomous-development")
    gm = GitManager(SafeExecutor(policy, state), "Agent", "agent@localhost")
    info = gm.prepare_workspace(user_repo, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", False, tmp_path / "b")
    (info.workspace_path / "feature.py").write_text("X = 1\n", encoding="utf-8")
    sha = gm.commit_changes(info.workspace_path, "feat: add feature", 5, {"main"}, ["Adds X.\nSecond line", "Validated acceptance criteria: X is 1"])
    message = _git_out(info.workspace_path, "log", "-1", "--format=%B")
    assert "Adds X. Second line" in message and "Validated acceptance criteria" in message and "Autonomous-Task: 5" in message
    ok, text = gm.push(info.workspace_path, "origin", "autonomous-development")
    assert ok, text
    assert _git_out(bare, "rev-parse", "autonomous-development") == sha
    assert _git_out(bare, "branch", "--list", "main") == ""  # main was never pushed


# ------------------------------------------------------- orchestrator push
def test_orchestrator_push_success_and_failure_never_breaks_run(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    bare = _bare_remote(tmp_path, project)
    orch, _, fake = _orchestrator(tmp_path, project, {}, git_push_enabled=True)
    orch._prepare_workspace()
    assert orch._push_ready
    (orch.workspace / "x.txt").write_text("x\n", encoding="utf-8")
    sha = orch.git.commit_changes(orch.workspace, "chore: x", None, {"main"})
    asyncio.run(orch._push("unit test"))
    assert _git_out(bare, "rev-parse", "autonomous-development") == sha
    events = [e["event_type"] for e in orch.state.events_after(0, 1000, orch.pid)]
    assert "GIT_PUSHED" in events

    # remote disappears -> failure is recorded and notified, nothing raises
    subprocess.run(["git", "remote", "set-url", "origin", str(tmp_path / "missing.git")], cwd=project, check=True)
    asyncio.run(orch._push("unit test 2"))
    assert any(e["event_type"] == "GIT_PUSH_FAILED" for e in orch.state.events_after(0, 1000, orch.pid))


def test_push_enabled_without_remote_disables_push_with_notice(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    orch, _, _ = _orchestrator(tmp_path, project, {}, git_push_enabled=True)
    orch._prepare_workspace()
    assert orch._push_ready is False
    assert any(e["event_type"] == "GIT_PUSH_FAILED" for e in orch.state.events_after(0, 1000, orch.pid))


# ----------------------------------------------------------- usage limits
CLI_LIMIT_MESSAGE = "Claude Code returned an error result: You've hit your 5-hour limit · resets 1:50am (Asia/Kolkata)"


def _limit_handlers(ref: dict, limit_once: bool = True, real_cli_error: bool = False):
    calls = {"developer": 0}

    def developer(call):
        calls["developer"] += 1
        if calls["developer"] == 1 and limit_once:
            if real_cli_error:
                from claude_agent_sdk import ProcessError

                # What the SDK really raises: the CLI prints the limit message and exits with code 1.
                # Press "Resume now" so the test does not wait until 1:50am.
                ref["state"].kv_set(f"limit_resume_now:{ref['pid']}", "1")
                raise ProcessError(CLI_LIMIT_MESSAGE, exit_code=1)
            raise UsageLimitReached("Claude usage limit reached (test)", time.time() + 1)
        call.write_file("inventory/stats.py", "VALUE = 1\n")
        ref["state"].set_flag(f"stop:{ref['pid']}", "1", "test")
        return {"status": "PASS", "summary": "done", "modified_files": ["inventory/stats.py"]}

    return {"product_manager": lambda c: _pm_output(False), "architect": _architect, "developer": developer, "code_reviewer": _reviewer_pass}, calls


def test_usage_limit_auto_resume_waits_and_reruns_stage(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    handlers, calls = _limit_handlers(ref)
    orch, _, fake = _orchestrator(tmp_path, project, handlers, auto_resume_on_limit=True)
    orch.limit_margin_seconds = 0
    ref.update(state=orch.state, pid=orch.pid)
    summary = asyncio.run(orch.run())
    task = orch.state.list_tasks(orch.pid)[0]
    assert summary["status"] == "STOPPED" and calls["developer"] == 2
    assert task["status"] == "IN_PROGRESS" and task["stage"] == "CODE_REVIEW" and task["stage_error_count"] == 0
    statuses = [(e["event_type"], e["status"]) for e in orch.state.events_after(0, 3000, orch.pid)]
    assert ("SYSTEM_PAUSED", "WAITING_FOR_LIMIT") in statuses and ("SYSTEM_RESUMED", "RUNNING") in statuses
    assert any("will resume automatically" in m["text"] for m in fake.messages)


def test_real_cli_limit_error_waits_instead_of_blocking(tmp_path):
    """Regression (cal-app3): the CLI's exit-code-1 limit error used to count as a stage error and BLOCK the task."""
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    handlers, calls = _limit_handlers(ref, real_cli_error=True)
    orch, _, _ = _orchestrator(tmp_path, project, handlers, auto_resume_on_limit=True)
    ref.update(state=orch.state, pid=orch.pid)
    summary = asyncio.run(orch.run())
    task = orch.state.list_tasks(orch.pid)[0]
    assert summary["status"] == "STOPPED" and calls["developer"] == 2  # no transient retries of a limit error
    assert task["status"] == "IN_PROGRESS" and task["stage"] == "CODE_REVIEW" and task["stage_error_count"] == 0
    paused = [e for e in orch.state.events_after(0, 3000, orch.pid) if e["event_type"] == "SYSTEM_PAUSED"]
    assert paused and "5-hour limit" in paused[0]["summary"]
    until = paused[0]["metadata"]["until"]
    assert 0 < until - time.time() <= 24 * 3600 + 120  # the parsed "resets 1:50am", not the 15-minute probe


def test_real_cli_limit_error_without_auto_resume_stops_not_blocks(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    handlers, calls = _limit_handlers(ref, real_cli_error=True)
    orch, _, _ = _orchestrator(tmp_path, project, handlers, auto_resume_on_limit=False)
    ref.update(state=orch.state, pid=orch.pid)
    summary = asyncio.run(orch.run())
    task = orch.state.list_tasks(orch.pid)[0]
    assert summary["status"] == "LIMIT_REACHED" and calls["developer"] == 1
    assert task["status"] == "IN_PROGRESS" and task["stage"] == "DEVELOPMENT"


def test_limit_text_detection_and_reset_parsing():
    from agent_runner import is_usage_limit_text, parse_reset_time

    assert is_usage_limit_text(CLI_LIMIT_MESSAGE)
    assert is_usage_limit_text("Claude usage limit reached (resets at unknown)")
    assert is_usage_limit_text("You've reached your weekly limit")
    assert not is_usage_limit_text("SDK process error: connection reset by peer")
    assert not is_usage_limit_text("tests failed: limit parameter must be positive")
    import datetime as dt

    base = dt.datetime(2026, 11, 26, 21, 40).timestamp()  # local time
    reset = dt.datetime.fromtimestamp(parse_reset_time("resets 1:50am", base))
    assert (reset.day, reset.hour, reset.minute) == (27, 1, 50)
    reset = dt.datetime.fromtimestamp(parse_reset_time("resets 11pm", base))
    assert (reset.day, reset.hour) == (26, 23)
    assert parse_reset_time("no time here") is None
    assert parse_reset_time(CLI_LIMIT_MESSAGE) is not None  # unknown zone without tzdata falls back to local time


def test_tasks_blocked_by_the_limit_are_requeued_on_start(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    handlers, _ = _limit_handlers(ref, limit_once=False)
    orch, _, _ = _orchestrator(tmp_path, project, handlers)
    ref.update(state=orch.state, pid=orch.pid)
    state, pid = orch.state, orch.pid
    state.upsert_project(pid, name="x", path=str(project), mode="existing", status="ACTIVE")
    limit_task, _ = state.create_task(pid, "limit task", "d")
    other_task, _ = state.create_task(pid, "broken task", "d")
    for task_id, reason in ((limit_task, "PRODUCT_VALIDATION failed 2 times: validator error: AgentError: SDK process error: " + CLI_LIMIT_MESSAGE),
                            (other_task, "tests keep failing after 3 repair attempts")):
        state.transition_task(task_id, to_status="BLOCKED", reason=reason, blocked_reason=reason)
    orch._recover()
    assert state.get_task(limit_task)["status"] == "PENDING" and state.get_task(limit_task)["blocked_reason"] is None
    assert state.get_task(other_task)["status"] == "BLOCKED"
    assert "#" + str(other_task) in orch._no_work_reason()


def test_usage_limit_without_auto_resume_stops_cleanly(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    ref: dict = {}
    handlers, calls = _limit_handlers(ref)
    orch, _, _ = _orchestrator(tmp_path, project, handlers, auto_resume_on_limit=False)
    ref.update(state=orch.state, pid=orch.pid)
    summary = asyncio.run(orch.run())
    task = orch.state.list_tasks(orch.pid)[0]
    assert summary["status"] == "LIMIT_REACHED" and calls["developer"] == 1
    assert task["status"] == "IN_PROGRESS" and task["stage"] == "DEVELOPMENT"  # resumes here on the next run


# ------------------------------------------------------------ stop modes
def test_manual_mode_idles_until_stopped(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    handlers = {"product_manager": lambda c: {"status": "PASS", "summary": "nothing to do"}}
    orch, _, _ = _orchestrator(tmp_path, project, handlers, stop_after="manual")
    seen_idle: dict = {}

    def stop_once_idle():
        from state_manager import StateManager

        watcher = StateManager(orch.state.db_path)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if any(e["status"] == "IDLE" for e in watcher.events_after(0, 2000, orch.pid)):
                seen_idle["at"] = time.monotonic()
                time.sleep(6)  # stays alive while idle (more than one poll interval)
                watcher.set_flag(f"stop:{orch.pid}", "1", "test")
                return
            time.sleep(0.5)

    threading.Thread(target=stop_once_idle, daemon=True).start()
    summary = asyncio.run(orch.run())
    assert seen_idle, "controller never reached the idle state"
    assert summary["status"] == "STOPPED" and time.monotonic() - seen_idle["at"] >= 6


# ---------------------------------------------------------- questions loop
def test_questions_answered_in_ui_reach_the_product_manager(tmp_path):
    project = copy_sample("flask-inventory", tmp_path)
    prompts: list[str] = []

    def pm(call):
        prompts.append(call.prompt)
        if len(prompts) == 1:
            return {"status": "BLOCKED", "summary": "need input", "questions": ["Should customers have a loyalty tier?"]}
        return {"status": "PASS", "summary": "ok"}

    orch, _, fake = _orchestrator(tmp_path, project, {"product_manager": pm})
    first = asyncio.run(orch.run())
    assert first["status"] == "AWAITING_INPUT"
    question_set = [a for a in orch.state.list_approvals(orch.pid) if a["kind"] == "QUESTIONS"][0]
    assert question_set["details"]["questions"] == ["Should customers have a loyalty tier?"]
    assert any("has questions" in m["text"] for m in fake.messages)

    ok, _ = TaskManager(orch.state, None, orch.pid, 3, False).apply_command("APPROVE", {"approval_id": question_set["id"], "note": "No loyalty tiers in version 1."}, "dashboard:admin")
    assert ok
    orch2, _, _ = _orchestrator(tmp_path, project, {"product_manager": pm})
    asyncio.run(orch2.run())
    assert "No loyalty tiers in version 1." in prompts[1] and "Should customers have a loyalty tier?" in prompts[1]
