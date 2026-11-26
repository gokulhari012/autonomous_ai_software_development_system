"""Per-agent tool permissions enforced by the controller.

The Claude Agent SDK is configured with ``permission_mode="dontAsk"`` and an
explicit tool list, but - as required - Claude's own permission settings are
*not* treated as the security boundary. Every tool call is checked by
:class:`ToolGuard` through a ``PreToolUse`` hook (which runs for every tool
call, including pre-approved ones) before it executes:

* only the agent's declared tools may be used;
* reads must stay inside the project workspace (plus explicitly shared report
  directories) and may not touch secret files;
* writes must match the agent's write scope (e.g. QA -> tests only, Code
  Reviewer / Security Auditor -> nothing) and never touch ``.git`` or secrets;
* Bash is limited to an allowlist of command prefixes (pytest, compileall)
  with no shell metacharacters and no paths outside the workspace.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

log = logging.getLogger("permissions")

READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# StructuredOutput is how Claude Code returns the JSON result requested with output_format;
# it only hands the agent's answer back to the orchestrator.
ALWAYS_ALLOWED = {"TodoWrite", "StructuredOutput"}
NEVER_ALLOWED = {
    "WebFetch", "WebSearch", "Task", "Agent", "KillShell", "KillBash", "BashOutput", "Skill",
    "ExitPlanMode", "EnterWorktree", "ExitWorktree", "CronCreate", "CronDelete", "RemoteTrigger",
    "SendMessage", "Monitor", "ScheduleWakeup",
}
SECRET_NAME_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "credentials*.json", "*.pfx", "*.p12", ".netrc", ".pypirc"]
ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}
PROTECTED_WRITE_GLOBS =[".git", ".git/**", "**/.git/**", ".env", "**/.env", ".env.*", "**/.env.*", ".autodev/**"]
SHELL_METACHARACTERS = re.compile(r"[;&|<>`]|\$\(|\$\{|\r|\n")

AGENT_POLICIES_DEFAULT_BASH: dict[str, list[list[str]]] = {
    "pytest": [["python", "-m", "pytest"], ["python3", "-m", "pytest"], ["py", "-m", "pytest"], ["pytest"]],
    "compileall": [["python", "-m", "compileall"], ["python3", "-m", "compileall"]],
}


@dataclass
class AgentPolicy:
    name: str
    tools: list[str]
    write_scope: list[str] = field(default_factory=list)
    bash_allow: list[str] = field(default_factory=list)  # keys of AGENT_POLICIES_DEFAULT_BASH

    def bash_prefixes(self) -> list[list[str]]:
        prefixes: list[list[str]] = []
        for key in self.bash_allow:
            prefixes.extend(AGENT_POLICIES_DEFAULT_BASH.get(key, []))
        return prefixes


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, pattern) for pattern in SECRET_NAME_PATTERNS) and lower not in ENV_TEMPLATE_NAMES


def _glob_match(rel_posix: str, pattern: str) -> bool:
    pattern = pattern.strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern in {"**", "**/*"}:
        return True
    if fnmatch.fnmatch(rel_posix, pattern):
        return True
    if pattern.endswith("/**"):
        base = pattern[:-3]
        return rel_posix == base or rel_posix.startswith(base + "/")
    if "**/" in pattern:
        return fnmatch.fnmatch(rel_posix, pattern.replace("**/", "")) or PurePosixPath(rel_posix).match(pattern)
    return False


class ToolGuard:
    """Decides whether a single tool call is permitted for an agent."""

    def __init__(
        self,
        policy: AgentPolicy,
        workspace: Path,
        write_scope: list[str] | None = None,
        extra_read_dirs: list[Path] | None = None,
        on_decision: Callable[[str, dict[str, Any], bool, str], None] | None = None,
    ):
        self.policy = policy
        self.workspace = workspace.resolve()
        self.write_scope = write_scope if write_scope is not None else list(policy.write_scope)
        self.extra_read_dirs = [d.resolve() for d in (extra_read_dirs or [])]
        self.on_decision = on_decision
        self.denials: list[dict[str, Any]] = []

    # -------------------------------------------------------------- helpers
    def _resolve(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def _inside(self, path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _relative(self, path: Path) -> str | None:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return None

    # --------------------------------------------------------------- checks
    def check(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        if tool_name in NEVER_ALLOWED or tool_name.startswith("mcp__"):
            return False, f"tool {tool_name} is not available to autonomous agents"
        if tool_name in ALWAYS_ALLOWED:
            return True, ""
        if tool_name not in self.policy.tools:
            return False, f"agent {self.policy.name} is not permitted to use {tool_name}"
        if tool_name in READ_TOOLS:
            return self._check_read(tool_name, tool_input)
        if tool_name in WRITE_TOOLS:
            return self._check_write(tool_input)
        if tool_name == "Bash":
            return self._check_bash(tool_input)
        return False, f"tool {tool_name} has no permission rule"

    def _check_read(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        candidates = [tool_input.get(k) for k in ("file_path", "path", "notebook_path") if tool_input.get(k)]
        pattern = tool_input.get("pattern") if tool_name == "Glob" else None
        if pattern:
            if ".." in Path(pattern).parts:
                return False, "glob patterns may not traverse outside the workspace"
            if Path(pattern).is_absolute():
                candidates.append(str(Path(pattern).parent))
        roots = [self.workspace, *self.extra_read_dirs]
        for value in candidates:
            path = self._resolve(str(value))
            if not self._inside(path, roots):
                return False, f"read access outside the project workspace is denied ({value})"
            if _is_secret_name(path.name):
                return False, f"reading secret file {path.name} is denied"
        glob_filter = tool_input.get("glob")
        if glob_filter and ".." in str(glob_filter):
            return False, "grep glob filters may not traverse outside the workspace"
        return True, ""

    def _check_write(self, tool_input: dict[str, Any]) -> tuple[bool, str]:
        value = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not value:
            return False, "write tool call without a file path"
        path = self._resolve(str(value))
        rel = self._relative(path)
        if rel is None:
            return False, f"writing outside the project workspace is denied ({value})"
        if _is_secret_name(path.name):
            return False, f"writing secret file {path.name} is denied"
        # Template files such as .env.example hold no secrets and are normal deliverables.
        if path.name.lower() not in ENV_TEMPLATE_NAMES and any(_glob_match(rel, g) for g in PROTECTED_WRITE_GLOBS):
            return False, f"{rel} is a protected path"
        if not self.write_scope:
            return False, f"agent {self.policy.name} has read-only access to application files"
        if not any(_glob_match(rel, g) for g in self.write_scope):
            return False, f"{rel} is outside this agent's write scope {self.write_scope}"
        return True, ""

    def _check_bash(self, tool_input: dict[str, Any]) -> tuple[bool, str]:
        command = str(tool_input.get("command", "")).strip()
        if tool_input.get("run_in_background"):
            return False, "background shell commands are not permitted"
        if not command:
            return False, "empty command"
        if SHELL_METACHARACTERS.search(command):
            return False, "shell operators, redirection and substitution are not permitted"
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            return False, f"unparseable command: {exc}"
        if not tokens:
            return False, "empty command"
        exe = PurePosixPath(tokens[0].replace("\\", "/")).name.lower().removesuffix(".exe")
        normalized = [exe, *tokens[1:]]
        if not any(normalized[: len(prefix)] == prefix for prefix in self.policy.bash_prefixes()):
            return False, f"command not on {self.policy.name}'s allowlist: {tokens[0]} {' '.join(tokens[1:3])}"
        for token in tokens[1:]:
            if token.startswith("-") and "=" in token:
                token = token.split("=", 1)[1]
            if token.startswith("-") or "::" in token and not ("/" in token or "\\" in token):
                continue
            if ".." in Path(token.split("::")[0]).parts:
                return False, "paths may not traverse outside the workspace"
            if "/" in token or "\\" in token or token.endswith(".py"):
                path = self._resolve(token.split("::")[0])
                if not self._inside(path, [self.workspace, *self.extra_read_dirs]):
                    return False, f"path {token} is outside the workspace"
        return True, ""

    # ------------------------------------------------------- SDK hook glue
    async def pre_tool_use_hook(self, hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or {}
        allowed, reason = self.check(tool_name, tool_input)
        if self.on_decision:
            try:
                self.on_decision(tool_name, tool_input, allowed, reason)
            except Exception:
                log.exception("permission decision callback failed")
        if allowed:
            return {}
        self.denials.append({"tool": tool_name, "reason": reason})
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Blocked by autonomous-dev policy: {reason}",
            }
        }


def expand_write_scope(raw: list[str], replacements: dict[str, list[str]]) -> list[str]:
    """Expand placeholders such as ``{test_dir}`` and ``{task_scope}`` in a write scope."""
    expanded: list[str] = []
    for entry in raw:
        matched = False
        for key, values in replacements.items():
            token = "{" + key + "}"
            if token in entry:
                matched = True
                for value in values:
                    expanded.append(entry.replace(token, value.strip("/")))
        if not matched:
            expanded.append(entry)
    return sorted({e.replace("\\", "/") for e in expanded if e})
