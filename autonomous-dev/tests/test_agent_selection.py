"""Agent team planning, automatic model selection and automatic upgrades (pure functions)."""

from __future__ import annotations

import pytest

from agent_selection import auto_model, escalate, failed_attempts, plan_team, task_complexity
from config import CORE_AGENTS, OPTIONAL_AGENTS, AgentTeamConfig, ProjectConfig


def _task(**fields):
    base = {"id": 1, "title": "Add stats endpoint", "kind": "feature", "description": "x" * 600,
            "acceptance_criteria": ["a", "b", "c", "d"], "file_scope": ["app/api.py", "app/db.py", "tests/test_api.py"]}
    base.update(fields)
    return base


SIMPLE = _task(kind="docs", title="Document setup", description="Update README", acceptance_criteria=["README explains setup"], file_scope=["README.md"])
COMPLEX = _task(kind="infrastructure", description="y" * 2000, acceptance_criteria=[str(i) for i in range(7)], file_scope=["**"])


def test_complexity_levels():
    assert task_complexity(SIMPLE)[0] == "simple"
    assert task_complexity(_task())[0] == "standard"
    assert task_complexity(COMPLEX)[0] == "complex"
    assert task_complexity(_task(), {"files": [f"f{i}.py" for i in range(12)], "database_changes": ["add table"]})[0] == "complex"
    assert task_complexity(None)[0] == "standard"


def test_full_and_custom_teams():
    assert plan_team(AgentTeamConfig(mode="full"), SIMPLE).agents == set(CORE_AGENTS) | set(OPTIONAL_AGENTS)
    custom = plan_team(AgentTeamConfig(mode="custom", agents=["qa_engineer"]), _task())
    assert custom.agents == set(CORE_AGENTS) | {"qa_engineer"}
    assert set(custom.skipped) == set(OPTIONAL_AGENTS) - {"qa_engineer"}


def test_auto_team_fits_the_task():
    auto = AgentTeamConfig(mode="auto")
    small = plan_team(auto, SIMPLE)
    assert {"architect", "qa_engineer", "security_auditor"} <= set(small.skipped)
    assert set(CORE_AGENTS) | {"bug_fixer", "devops_engineer"} <= small.agents  # gates and on-demand agents stay
    login = plan_team(auto, _task(title="Fix login typo", kind="bugfix", description="Login page typo", acceptance_criteria=["fixed"], file_scope=["app/login.py"]))
    assert login.has("security_auditor") and not login.has("architect")  # security-sensitive, even though simple
    assert plan_team(auto, COMPLEX).agents == set(CORE_AGENTS) | set(OPTIONAL_AGENTS)
    assert plan_team(auto, None).has("architect")  # new-project architecture


def test_auto_model_and_upgrade_ladder():
    assert auto_model("developer", "simple") == ("claude-sonnet-5-5", "medium")
    assert auto_model("developer", "complex") == ("claude-opus-5-5", "xhigh")
    assert auto_model("qa_engineer", "standard") == ("claude-opus-5-5", "medium")  # one level lower

    assert escalate("claude-sonnet-5-5", "medium", 0, "claude-opus-5-5") == ("claude-sonnet-5-5", "medium")
    assert escalate("claude-sonnet-5-5", "medium", 1, "claude-opus-5-5") == ("claude-sonnet-5-5", "high")
    assert escalate("claude-sonnet-5-5", "high", 1, "claude-opus-5-5") == ("claude-opus-5-5", "high")
    assert escalate("claude-opus-5-5", "xhigh", 1, "claude-opus-5-5") == ("claude-opus-5-5", "max")
    assert escalate("claude-opus-5-5", "max", 5, "claude-opus-5-5") == ("claude-opus-5-5", "max")  # at the limit
    assert escalate("claude-opus-5-5", "xhigh", 1, "claude-fable-5-2") == ("claude-fable-5-2", "high")
    assert escalate("claude-opus-5-5", "high", 1, "claude-sonnet-5-5") == ("claude-opus-5-5", "high")  # never downgrades
    assert escalate("claude-haiku-5-5", "low", 1, "claude-sonnet-5-5") == ("claude-haiku-5-5", "medium")


def test_failed_attempts_counts_repairs_only_for_builders():
    task = {"retry_count": 2, "stage_error_count": 1}
    assert failed_attempts("developer", task) == 3 and failed_attempts("bug_fixer", task) == 3
    assert failed_attempts("code_reviewer", task) == 1
    assert failed_attempts("developer", None) == 0


def test_agent_team_config_validation():
    assert ProjectConfig(project_name="p", project_path=".").agent_team.mode == "full"
    with pytest.raises(Exception):
        AgentTeamConfig(mode="custom", agents=["developer"])  # core agents cannot be listed (they always run)
    with pytest.raises(Exception):
        AgentTeamConfig(mode="sometimes")
    assert AgentTeamConfig(mode="custom", agents=["devops_engineer", "architect"]).agents == ["architect", "devops_engineer"]
