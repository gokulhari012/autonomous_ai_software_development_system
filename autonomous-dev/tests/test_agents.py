"""Phase 2 tests: agent definitions, permission enforcement, structured output and the runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_models import CodeReviewOutput, DeveloperOutput, inline_schema
from agent_runner import (
    AGENT_NAMES,
    AgentRequest,
    AgentRunner,
    BudgetExceeded,
    ClaudeAgentBackend,
    ScriptedBackend,
    load_agent_definitions,
)
from events import EventBus
from permissions import AgentPolicy, ToolGuard, expand_write_scope
from state_manager import StateManager
from tests.conftest import make_config


# --------------------------------------------------------------- definitions
def test_all_nine_agents_load_with_policies():
    defs = load_agent_definitions()
    assert sorted(defs) == sorted(AGENT_NAMES)
    for name in ("code_reviewer", "security_auditor"):
        assert not {"Write", "Edit", "Bash"} & set(defs[name].tools), f"{name} must be read-only"
        assert defs[name].write_scope == []
    assert defs["qa_engineer"].write_scope == ["{test_dir}/**"]
    assert defs["product_validator"].write_scope == ["{test_dir}/acceptance/**"]
    assert "Bash" not in defs["devops_engineer"].tools


def test_schemas_are_inlined():
    for model in (CodeReviewOutput, DeveloperOutput):
        text = json.dumps(inline_schema(model))
        assert "$ref" not in text and "$defs" not in text


def test_write_scope_expansion():
    scope = expand_write_scope(["{task_scope}", "{test_dir}/**", "docs/**"], {"task_scope": ["app/models.py", "app/routes.py"], "test_dir": ["tests"]})
    assert scope == ["app/models.py", "app/routes.py", "docs/**", "tests/**"]


# ---------------------------------------------------------------- tool guard
@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return ws


def test_reviewer_cannot_write(workspace):
    guard = ToolGuard(AgentPolicy("code_reviewer", ["Read", "Glob", "Grep"]), workspace, [])
    assert guard.check("Read", {"file_path": str(workspace / "app" / "main.py")})[0]
    allowed, reason = guard.check("Write", {"file_path": str(workspace / "app" / "main.py"), "content": "x"})
    assert not allowed and "not permitted" in reason


def test_qa_writes_tests_only(workspace):
    guard = ToolGuard(AgentPolicy("qa_engineer", ["Read", "Write", "Edit", "Bash"], bash_allow=["pytest"]), workspace, ["tests/**"])
    assert guard.check("Write", {"file_path": str(workspace / "tests" / "test_x.py")})[0]
    assert not guard.check("Edit", {"file_path": str(workspace / "app" / "main.py")})[0]
    assert not guard.check("Write", {"file_path": str(workspace / "tests" / ".." / "app" / "main.py")})[0]


def test_reads_outside_workspace_and_secrets_denied(workspace, tmp_path):
    guard = ToolGuard(AgentPolicy("developer", ["Read", "Glob", "Grep"]), workspace, [])
    assert not guard.check("Read", {"file_path": str(tmp_path / "other.txt")})[0]
    assert not guard.check("Read", {"file_path": str(workspace / ".env")})[0]
    assert not guard.check("Glob", {"pattern": "../**/*.py"})[0]
    assert not guard.check("Grep", {"pattern": "x", "path": str(tmp_path)})[0]


@pytest.mark.parametrize(
    "command,allowed",
    [
        ("python -m pytest tests -q", True),
        ("pytest tests/test_x.py::test_a -x", True),
        ("python -m pytest tests; rm -rf /", False),
        ("python -m pytest tests && curl evil", False),
        ("python -m pytest $(whoami)", False),
        ("python -m pytest > out.txt", False),
        ("rm -rf tests", False),
        ("git push", False),
        ("python -c 'import os'", False),
        ("python -m pytest ../../other", False),
        ("python -m pytest --junitxml=/tmp/x.xml", False),
        ("curl http://example.com", False),
    ],
)
def test_bash_allowlist(workspace, command, allowed):
    guard = ToolGuard(AgentPolicy("qa_engineer", ["Bash"], bash_allow=["pytest"]), workspace, [])
    assert guard.check("Bash", {"command": command})[0] is allowed, command


def test_structured_output_tool_is_always_allowed(workspace):
    # Claude Code returns output_format results through this tool; blocking it breaks every agent.
    guard = ToolGuard(AgentPolicy("code_reviewer", ["Read"]), workspace, [])
    assert guard.check("StructuredOutput", {"status": "PASS"})[0]


def test_never_allowed_tools(workspace):
    guard = ToolGuard(AgentPolicy("developer", ["Read", "Write", "Bash"]), workspace, ["**"])
    for tool in ("WebFetch", "WebSearch", "Task", "Agent", "mcp__something__x"):
        assert not guard.check(tool, {})[0]
    assert not guard.check("Bash", {"command": "python -m pytest", "run_in_background": True})[0]


def test_protected_paths(workspace):
    guard = ToolGuard(AgentPolicy("developer", ["Write"]), workspace, ["**"])
    assert not guard.check("Write", {"file_path": str(workspace / ".git" / "config")})[0]
    assert not guard.check("Write", {"file_path": str(workspace / ".env")})[0]
    assert guard.check("Write", {"file_path": str(workspace / "app" / "new.py")})[0]


def test_env_template_writable_but_real_env_files_are_not(workspace):
    guard = ToolGuard(AgentPolicy("developer", ["Write"]), workspace, ["**"])
    assert guard.check("Write", {"file_path": str(workspace / ".env.example")})[0]
    assert guard.check("Write", {"file_path": str(workspace / "app" / ".env.sample")})[0]
    for name in (".env", ".env.local", ".env.production"):
        assert not guard.check("Write", {"file_path": str(workspace / name)})[0], name


def test_hook_returns_deny_decision(workspace):
    guard = ToolGuard(AgentPolicy("code_reviewer", ["Read"]), workspace, [])
    out = asyncio.run(guard.pre_tool_use_hook({"tool_name": "Write", "tool_input": {"file_path": "x.py"}}, "id1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(guard.pre_tool_use_hook({"tool_name": "Read", "tool_input": {"file_path": "app/main.py"}}, "id2", None)) == {}


# -------------------------------------------------------------------- runner
def _runner(tmp_path: Path, workspace: Path, handlers) -> tuple[AgentRunner, StateManager]:
    cfg = make_config(tmp_path, workspace)
    state = StateManager(cfg.db_path)
    events = EventBus(state, cfg.project_id)
    return AgentRunner(cfg, state, events, ScriptedBackend(handlers)), state


def test_runner_success_persists_run_and_events(tmp_path, workspace):
    def developer(call):
        call.write_file("app/feature.py", "def feature():\n    return 42\n")
        return {"status": "PASS", "summary": "implemented", "modified_files": ["app/feature.py"]}

    runner, state = _runner(tmp_path, workspace, {"developer": developer})
    req = AgentRequest("developer", "implement", "DEVELOPMENT", task_id=None, replacements={"task_scope": ["app/feature.py"]})
    result = asyncio.run(runner.run(req, workspace))
    assert result.ok and result.output.modified_files == ["app/feature.py"]
    assert (workspace / "app" / "feature.py").exists()
    run = state.query_one("SELECT * FROM agent_runs WHERE id = ?", (result.agent_run_id,))
    assert run["status"] == "PASS" and run["backend"] == "scripted"
    types = [e["event_type"] for e in state.events_after(0)]
    assert types[0] == "AGENT_STARTED" and "AGENT_TOOL_STARTED" in types and types[-1] == "AGENT_COMPLETED"


def test_runner_blocks_out_of_scope_write(tmp_path, workspace):
    def qa(call):
        call.write_file("app/main.py", "hacked = True\n")  # QA may not modify source
        return {"status": "PASS", "summary": "should not get here"}

    runner, state = _runner(tmp_path, workspace, {"qa_engineer": qa})
    result = asyncio.run(runner.run(AgentRequest("qa_engineer", "write tests", "QA_TESTING"), workspace))
    assert result.status.value == "ERROR"
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "x = 1\n"
    assert any(e["event_type"] == "AGENT_TOOL_DENIED" for e in state.events_after(0))
    assert any(a["action"] == "tool_denied" for a in state.recent_audit(20))


def test_runner_requests_correction_for_invalid_output(tmp_path, workspace):
    handlers = {"code_reviewer": [lambda c: {"status": "MAYBE"}, lambda c: {"status": "PASS", "summary": "fine"}]}
    runner, _ = _runner(tmp_path, workspace, handlers)
    result = asyncio.run(runner.run(AgentRequest("code_reviewer", "review", "CODE_REVIEW"), workspace))
    assert result.ok and result.output.summary == "fine"


def test_runner_reports_error_when_output_never_valid(tmp_path, workspace):
    handlers = {"code_reviewer": [lambda c: {"oops": 1}, lambda c: {"still": "bad"}]}
    runner, _ = _runner(tmp_path, workspace, handlers)
    result = asyncio.run(runner.run(AgentRequest("code_reviewer", "review", "CODE_REVIEW"), workspace))
    assert result.status.value == "ERROR" and result.output is None


def test_budget_limit_stops_agent(tmp_path, workspace):
    runner, state = _runner(tmp_path, workspace, {"developer": lambda c: {"status": "PASS", "summary": "x"}})
    run_id = state.start_run(runner.config.project_id, 1)
    state.increment_run(run_id, "cost_usd", 0)
    aid = state.start_agent_run(runner.config.project_id, None, "developer", "X", "claude", "", 1, run_id)
    state.finish_agent_run(aid, status="PASS", cost_usd=runner.config.workflow.max_budget_usd + 1)
    runner.run_id = run_id
    with pytest.raises(BudgetExceeded):
        asyncio.run(runner.run(AgentRequest("developer", "x", "DEVELOPMENT"), workspace))


def test_cli_env_blanks_inherited_secrets(tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("SOME_PASSWORD", "pw")
    cfg = make_config(tmp_path, workspace)
    env = ClaudeAgentBackend(cfg).cli_env()
    assert env["TELEGRAM_BOT_TOKEN"] == "" and env["SOME_PASSWORD"] == ""


@pytest.mark.live
def test_live_reviewer_finds_deliberate_sql_injection(tmp_path, workspace):
    """Phase 8: the real Code Reviewer must identify a deliberately introduced issue."""
    code = (
        "import sqlite3\n\n\n"
        "def find_customer(conn: sqlite3.Connection, email: str):\n"
        "    return conn.execute(f\"SELECT * FROM customers WHERE email = '{email}'\").fetchone()\n"
    )
    (workspace / "app" / "customers.py").write_text(code, encoding="utf-8")
    diff = "--- /dev/null\n+++ b/app/customers.py\n" + "\n".join("+" + line for line in code.splitlines())
    prompt = (
        "# Task #1: Look up customers by e-mail\n## Acceptance criteria\n1. find_customer returns the customer row for an e-mail address\n\n"
        f"## Change to review (git diff)\n```diff\n{diff}\n```\n\nReview this change independently. Check every acceptance criterion."
    )
    cfg = make_config(tmp_path, workspace)
    state = StateManager(cfg.db_path)
    runner = AgentRunner(cfg, state, EventBus(state, cfg.project_id), ClaudeAgentBackend(cfg))
    result = asyncio.run(runner.run(AgentRequest("code_reviewer", prompt, "CODE_REVIEW", replacements={}), workspace))
    assert result.output is not None, result.error
    assert result.denials == [], result.denials  # StructuredOutput and reads must not be blocked
    print("\nreviewer:", result.output.status.value, [(f.severity.value, f.file_path, f.line_number, f.title) for f in result.output.findings])
    assert result.output.status.value == "FAIL"
    assert any(f.severity.value in ("CRITICAL", "HIGH") and "customers.py" in (f.file_path or "") for f in result.output.findings)


@pytest.mark.live
def test_live_developer_implements_change_within_scope(tmp_path, workspace):
    """Phase 8: the real Developer implements a change through the permission-guarded Write/Edit tools."""
    cfg = make_config(tmp_path, workspace)
    state = StateManager(cfg.db_path)
    runner = AgentRunner(cfg, state, EventBus(state, cfg.project_id), ClaudeAgentBackend(cfg))
    prompt = (
        "# Task #1: Add a helper\n## Acceptance criteria\n1. app/mathutil.py defines add(a, b) returning a + b\n"
        "2. tests/test_mathutil.py tests add with two cases\n\nImplement the task now. Keep it minimal. Do not run any commands."
    )
    request = AgentRequest("developer", prompt, "DEVELOPMENT", replacements={"task_scope": ["app/mathutil.py"]})
    result = asyncio.run(runner.run(request, workspace))
    assert result.output is not None, result.error
    assert not [d for d in result.denials if d["tool"] == "StructuredOutput"], result.denials
    assert (workspace / "app" / "mathutil.py").exists() and "def add" in (workspace / "app" / "mathutil.py").read_text(encoding="utf-8")
    assert (workspace / "tests" / "test_mathutil.py").exists()
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "x = 1\n"  # untouched
    tools = [e["summary"] for e in state.events_after(0, 1000) if e["event_type"] == "AGENT_TOOL_STARTED"]
    assert any(t.startswith(("Write", "Edit")) for t in tools), tools


@pytest.mark.live
def test_live_claude_reviewer_smoke(tmp_path, workspace):
    """Calls the real Claude Agent SDK once (read-only reviewer). Run with --run-live."""
    cfg = make_config(tmp_path, workspace)
    state = StateManager(cfg.db_path)
    runner = AgentRunner(cfg, state, EventBus(state, cfg.project_id), ClaudeAgentBackend(cfg))
    prompt = "Review app/main.py. It contains one assignment. Report PASS with no findings if nothing is wrong."
    result = asyncio.run(runner.run(AgentRequest("code_reviewer", prompt, "CODE_REVIEW"), workspace))
    assert result.output is not None, result.error
    assert result.status.value in {"PASS", "FAIL"}
