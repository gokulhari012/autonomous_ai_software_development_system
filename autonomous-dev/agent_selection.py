"""Which agents run for a task, and which Claude model and effort each agent call uses.

Agent team (``project.agent_team``):

* ``full``   - every agent runs.
* ``custom`` - the core agents plus the optional agents the owner ticked.
* ``auto``   - the core agents plus the optional agents the task needs, judged from its complexity.

What replaces an optional agent that is not on the team:

* architect        - the developer works from the task description (no design step).
* qa_engineer      - the orchestrator still runs the full test suite; failures go straight to repair.
* security_auditor - the scanners (Bandit, Semgrep, pip-audit) and the blocking policy still run.
* bug_fixer        - the developer fixes the open issues instead.
* devops_engineer  - a failed staging start is recorded as an issue and sent for repair.

Model and effort (``workflow.auto_model_selection`` / ``workflow.auto_escalation``): see
:func:`auto_model` and :func:`escalate`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from config import CORE_AGENTS, OPTIONAL_AGENTS, AgentTeamConfig

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
COMPLEXITIES = ("simple", "standard", "complex")

# Base model and effort for each task complexity when automatic selection is on.
COMPLEXITY_MODEL: dict[str, tuple[str, str]] = {
    "simple": ("claude-sonnet-5-5", "medium"),
    "standard": ("claude-opus-5-5", "high"),
    "complex": ("claude-opus-5-5", "xhigh"),
}
# Agents whose work is mostly mechanical run one effort level lower than the table.
LIGHT_AGENTS = {"qa_engineer", "devops_engineer"}
# Agents that produce the change: when their work is sent back for repair, they are the ones that fell short.
BUILDER_AGENTS = {"developer", "bug_fixer"}

# Strength order used for automatic upgrades. The last step at the ceiling model is effort "max".
UPGRADE_LADDER: list[tuple[str, str]] = [
    ("claude-haiku-5-5", "medium"), ("claude-haiku-5-5", "high"),
    ("claude-sonnet-5-5", "medium"), ("claude-sonnet-5-5", "high"),
    ("claude-opus-5-5", "high"), ("claude-opus-5-5", "xhigh"),
    ("claude-fable-5-2", "high"), ("claude-fable-5-2", "xhigh"),
]

SECURITY_WORDS = re.compile(
    r"\b(auth\w*|log ?in|logout|password\w*|token\w*|session\w*|permission\w*|roles?|upload\w*|secret\w*|encrypt\w*|"
    r"crypt\w*|csrf|xss|sql|injection|admin\w*|payment\w*|credential\w*|oauth|jwt)\b",
    re.I,
)


def model_rank(model: str | None) -> int:
    """Rough capability tier of a model id (unknown ids count as Opus tier)."""
    name = (model or "").lower()
    for word, rank in (("haiku", 0), ("sonnet", 1), ("opus", 2), ("fable", 3), ("mythos", 3)):
        if word in name:
            return rank
    return 2


def effort_rank(effort: str | None) -> int:
    return EFFORTS.index(effort) if effort in EFFORTS else EFFORTS.index("high")


# ------------------------------------------------------------------ complexity
def task_complexity(task: dict[str, Any] | None, design: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """Classify a task as simple / standard / complex from what the plan says about it."""
    if not task:
        return "standard", ["project-level planning"]
    score = 0
    reasons: list[str] = []
    kind = task.get("kind") or "feature"
    kind_score = {"docs": -2, "test": -1, "bugfix": -1, "refactor": 1, "infrastructure": 1, "security": 1}.get(kind, 0)
    if kind_score:
        score += kind_score
        reasons.append(f"{kind} task")
    criteria = list(task.get("acceptance_criteria") or [])
    if len(criteria) <= 2:
        score -= 1
        reasons.append(f"{len(criteria)} acceptance criteria")
    elif len(criteria) >= 10:
        score += 2
        reasons.append(f"{len(criteria)} acceptance criteria")
    elif len(criteria) >= 6:
        score += 1
        reasons.append(f"{len(criteria)} acceptance criteria")
    description = task.get("description") or ""
    if len(description) < 300:
        score -= 1
        reasons.append("short description")
    elif len(description) > 1500:
        score += 1
        reasons.append("long description")
    files = list((design or {}).get("files") or task.get("file_scope") or [])
    if "**" in files or len(files) > 8:
        score += 1
        reasons.append("touches many files" if files != ["**"] else "whole-project scope")
    elif 0 < len(files) <= 2:
        score -= 1
        reasons.append(f"{len(files)} file(s) in scope")
    if (design or {}).get("database_changes"):
        score += 1
        reasons.append("database changes")
    level = "simple" if score <= -2 else "complex" if score >= 2 else "standard"
    return level, reasons


# ------------------------------------------------------------------ agent team
@dataclass
class TeamPlan:
    agents: set[str]
    mode: str
    reason: str = ""
    skipped: dict[str, str] = field(default_factory=dict)  # agent -> why it is not on the team

    def has(self, agent: str) -> bool:
        return agent in self.agents

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "agents": sorted(self.agents), "skipped": self.skipped, "reason": self.reason}


def plan_team(team: AgentTeamConfig, task: dict[str, Any] | None, complexity: str | None = None) -> TeamPlan:
    core = set(CORE_AGENTS)
    if team.mode == "full":
        return TeamPlan(core | set(OPTIONAL_AGENTS), "full", "full team")
    if team.mode == "custom":
        chosen = set(team.agents)
        skipped = {a: "not selected for this project" for a in OPTIONAL_AGENTS if a not in chosen}
        return TeamPlan(core | chosen, "custom", "custom team", skipped)

    # auto
    if task is None:  # project-level work (new-project architecture) keeps the architect
        return TeamPlan(core | set(OPTIONAL_AGENTS), "auto", "project planning uses the full team")
    level = complexity or task_complexity(task)[0]
    kind = task.get("kind") or "feature"
    text = " ".join([task.get("title") or "", task.get("description") or "", " ".join(task.get("acceptance_criteria") or [])])
    sensitive = kind == "security" or bool(SECURITY_WORDS.search(text))
    skipped: dict[str, str] = {}
    if level == "simple":
        skipped["architect"] = "simple task"
    elif kind in ("docs", "test", "bugfix") and level != "complex":
        skipped["architect"] = f"{kind} task needs no design step"
    if kind == "docs":
        skipped["qa_engineer"] = "documentation task"
    elif level == "simple":
        skipped["qa_engineer"] = "simple task: developer tests, the full suite and acceptance tests cover it"
    if not sensitive and (level == "simple" or kind in ("docs", "test")):
        skipped["security_auditor"] = "no security-sensitive change (scanners still run)"
    # bug_fixer and devops_engineer only run when something fails, so they cost nothing otherwise.
    agents = core | {a for a in OPTIONAL_AGENTS if a not in skipped}
    reason = f"{level} {kind} task" + (" touching security-sensitive code" if sensitive else "")
    return TeamPlan(agents, "auto", reason, skipped)


# ------------------------------------------------------------- model / effort
def auto_model(agent: str, complexity: str) -> tuple[str, str]:
    model, effort = COMPLEXITY_MODEL.get(complexity, COMPLEXITY_MODEL["standard"])
    if agent in LIGHT_AGENTS:
        effort = EFFORTS[max(0, effort_rank(effort) - 1)]
    return model, effort


def escalate(model: str | None, effort: str | None, steps: int, ceiling: str) -> tuple[str | None, str | None]:
    """Move ``steps`` places up the upgrade ladder from (model, effort), never above ``ceiling``.

    Never downgrades: if the starting point is already at or above the ceiling, it is kept."""
    if steps <= 0:
        return model, effort
    top = model_rank(ceiling)
    ladder = [entry for entry in UPGRADE_LADDER if model_rank(entry[0]) <= top]
    ladder.append((ladder[-1][0], "max"))
    start = (model_rank(model), effort_rank(effort))
    stronger = [entry for entry in ladder if (model_rank(entry[0]), effort_rank(entry[1])) > start]
    if not stronger:
        return model, effort
    return stronger[min(steps, len(stronger)) - 1]


def failed_attempts(agent: str, task: dict[str, Any] | None) -> int:
    """How many times this agent has already fallen short on the task (drives automatic upgrades)."""
    if not task:
        return 0
    attempts = int(task.get("stage_error_count") or 0)  # errors / invalid output in the current stage
    if agent in BUILDER_AGENTS:
        attempts += int(task.get("retry_count") or 0)  # its change was sent back by a review or test gate
    return attempts
