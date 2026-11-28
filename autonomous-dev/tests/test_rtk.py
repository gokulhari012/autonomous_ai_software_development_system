"""RTK (Rust Token Killer) support: rewrite acceptance, permission hook, settings and detection."""

from __future__ import annotations

import asyncio

import pytest

from config import WorkflowConfig
from permissions import AgentPolicy, ToolGuard
from rtk_support import accept_rewrite, check_rtk


def test_accept_rewrite_only_allows_rtk_pytest_with_original_arguments():
    assert accept_rewrite("python -m pytest -q tests/test_a.py", "rtk pytest -q tests/test_a.py") == "rtk pytest -q tests/test_a.py"
    assert accept_rewrite("pytest -x", "rtk pytest -x") == "rtk pytest -x"
    assert accept_rewrite("python -m pytest", "python -m pytest") is None  # no change
    assert accept_rewrite("python -m pytest", "") is None
    assert accept_rewrite("python -m pytest", "rtk pytest; rm -rf .") is None  # shell operators
    assert accept_rewrite("python -m pytest", "rtk pytest ../other") is None  # argument not in the original
    assert accept_rewrite("python -m pytest", "rtk bypass rm x") is None  # subcommand not accepted
    assert accept_rewrite("python -m compileall .", "rtk python -m compileall .") is None
    assert accept_rewrite("python -m pytest -q", "rtk pytest -q -q") is None  # each argument at most as often as before


def _guard(workspace, rewriter):
    return ToolGuard(AgentPolicy("qa_engineer", ["Bash"], bash_allow=["pytest"]), workspace, [], bash_rewriter=rewriter)


def test_hook_rewrites_only_allowed_commands(tmp_path):
    seen: list[str] = []

    async def rewriter(command: str) -> str | None:
        seen.append(command)
        return "rtk pytest -q"

    guard = _guard(tmp_path, rewriter)
    out = asyncio.run(guard.pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "python -m pytest -q", "description": "tests"}}, None, None))
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow" and spec["updatedInput"] == {"command": "rtk pytest -q", "description": "tests"}

    denied = asyncio.run(guard.pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "pip install requests"}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen == ["python -m pytest -q"]  # RTK is never asked about a denied command


def test_hook_falls_back_to_the_original_command(tmp_path):
    async def nothing(command: str) -> str | None:
        return None

    async def broken(command: str) -> str | None:
        raise RuntimeError("rtk crashed")

    for rewriter in (nothing, broken):
        out = asyncio.run(_guard(tmp_path, rewriter).pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "python -m pytest"}}, None, None))
        assert out == {}  # allowed, unchanged


def test_rtk_recall_is_allowed_only_with_rtk_on(tmp_path):
    async def rewriter(command: str) -> str | None:
        return None

    assert _guard(tmp_path, rewriter).check("Bash", {"command": "rtk recall a1b2c3d4"})[0]
    assert not _guard(tmp_path, None).check("Bash", {"command": "rtk recall a1b2c3d4"})[0]
    assert not _guard(tmp_path, rewriter).check("Bash", {"command": "rtk bypass pip install x"})[0]
    assert not _guard(tmp_path, rewriter).check("Bash", {"command": "rtk recall abcd; whoami"})[0]
    no_bash = ToolGuard(AgentPolicy("developer", ["Bash"]), tmp_path, [], bash_rewriter=rewriter)
    assert not no_bash.check("Bash", {"command": "rtk recall a1b2c3d4"})[0]  # agents without shell commands get none


def test_rtk_settings_validation(tmp_path):
    assert WorkflowConfig().rtk_enabled is False and WorkflowConfig().rtk_path is None
    assert WorkflowConfig(rtk_path="").rtk_path is None
    exe = str(tmp_path / "rtk.exe")
    assert WorkflowConfig(rtk_enabled=True, rtk_path=exe).rtk_path == exe
    for bad in ("rtk.exe", str(tmp_path / "evil.exe")):
        with pytest.raises(Exception):
            WorkflowConfig(rtk_path=bad)


def test_check_rtk_reports_a_missing_program(tmp_path):
    status = check_rtk(str(tmp_path / "rtk.exe"))
    assert not status.usable and "not found" in status.detail and "winget install rtk-ai.rtk" in status.detail
