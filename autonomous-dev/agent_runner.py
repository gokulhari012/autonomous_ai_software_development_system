"""AgentRunner: executes the specialised agents through the Claude Agent SDK.

Responsibilities:

* load agent definitions (``agents/*.md`` - YAML front matter + instructions);
* build ``ClaudeAgentOptions`` per agent: restricted tool list, ``dontAsk``
  permission mode, a ``PreToolUse`` hook running :class:`permissions.ToolGuard`,
  no filesystem settings (``setting_sources=[]``), structured JSON output,
  turn and budget limits, a scrubbed environment;
* stream SDK messages and publish *actual* agent activity as events
  (tool started / completed / denied, progress text, rate limits, usage);
* enforce timeouts, bounded retries with exponential backoff for transient
  failures, and validation of the structured output with Pydantic;
* persist every run (status, usage, cost, turns) in ``agent_runs``.

``ScriptedBackend`` is a deterministic test double used only by the
controller's own test-suite and the offline demo. Every run it performs is
recorded with ``backend='scripted'`` so it can never be mistaken for real
Claude output.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from pydantic import ValidationError

from agent_models import OUTPUT_MODELS, AgentOutputBase, Status, inline_schema
from config import AGENTS_DIR, DATABASE_DIR, AppConfig
from events import EventBus
from logging_setup import redact
from permissions import NEVER_ALLOWED, AgentPolicy, ToolGuard, expand_write_scope
from rtk_support import RtkRewriter, check_rtk, rtk_env
from state_manager import StateManager

log = logging.getLogger("agent_runner")

AGENT_NAMES = [
    "product_manager", "architect", "developer", "code_reviewer", "qa_engineer",
    "security_auditor", "bug_fixer", "devops_engineer", "product_validator",
]
KNOWN_TOOLS = {"Read", "Glob", "Grep", "Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"}
AUTH_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "AWS_", "GOOGLE_", "CLOUD_ML_", "AZURE_", "VERTEX_")
SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "CREDENTIAL", "PRIVATE_KEY")
RETRYABLE_HTTP = {408, 409, 429, 500, 502, 503, 504, 529}

# Text the Claude CLI prints when a plan limit is hit, e.g.
# "You've hit your 5-hour limit · resets 1:50am (Asia/Kolkata)". The CLI then exits with code 1,
# so the SDK raises ProcessError and the ResultMessage is never seen.
USAGE_LIMIT_RE = re.compile(
    r"\b(?:hit|reached|exceeded)\s+your\s+[^.\n]{0,40}?\blimit\b|\busage\s+limit\b|\b(?:5|five)-hour\s+limit\b|\bweekly\s+limit\b",
    re.I,
)
RESET_TIME_RE = re.compile(r"\bresets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2})\))?", re.I)


def is_usage_limit_text(text: str | None) -> bool:
    return bool(text and USAGE_LIMIT_RE.search(text))


def parse_reset_time(text: str | None, now: float | None = None) -> float | None:
    """Unix timestamp of the next "resets 1:50am (Asia/Kolkata)" time in ``text``; None if absent.

    Falls back to the computer's local time zone when the named zone is unknown (no tzdata)."""
    match = RESET_TIME_RE.search(text or "")
    if not match:
        return None
    import datetime as dt

    hour, minute, ampm, zone_name = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower(), match.group(4)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = None
    if zone_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(zone_name)
        except Exception:
            tz = None
    current = dt.datetime.fromtimestamp(now if now is not None else time.time(), tz=tz).astimezone(tz) if tz else dt.datetime.fromtimestamp(now if now is not None else time.time())
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += dt.timedelta(days=1)
    return candidate.timestamp()


class AgentError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class FatalAgentError(AgentError):
    """Errors that will affect every agent call (authentication, billing, missing CLI)."""


class BudgetExceeded(AgentError):
    pass


class TurnLimitExceeded(AgentError):
    pass


class UsageLimitReached(AgentError):
    """The Claude plan/API usage limit was hit. ``resets_at`` is a unix timestamp when known."""

    def __init__(self, message: str, resets_at: float | None = None):
        super().__init__(message, retryable=False)
        self.resets_at = resets_at


# ---------------------------------------------------------------- definitions
@dataclass
class AgentDefinition:
    name: str
    description: str
    tools: list[str]
    write_scope: list[str]
    bash_allow: list[str]
    output_model: type[AgentOutputBase]
    instructions: str
    path: Path

    def policy(self) -> AgentPolicy:
        return AgentPolicy(self.name, list(self.tools), list(self.write_scope), list(self.bash_allow))


def parse_agent_file(path: Path) -> AgentDefinition:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not match:
        raise ValueError(f"{path.name}: missing YAML front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    for key in ("name", "description", "tools", "output_model"):
        if key not in meta:
            raise ValueError(f"{path.name}: front matter is missing {key!r}")
    unknown = set(meta["tools"]) - KNOWN_TOOLS
    if unknown:
        raise ValueError(f"{path.name}: unknown tools {sorted(unknown)}")
    if meta["output_model"] not in OUTPUT_MODELS:
        raise ValueError(f"{path.name}: unknown output_model {meta['output_model']}")
    write_scope = list(meta.get("write_scope") or [])
    if write_scope and not any(t in meta["tools"] for t in ("Write", "Edit", "MultiEdit")):
        raise ValueError(f"{path.name}: write_scope given but no write tools")
    return AgentDefinition(
        name=meta["name"],
        description=meta["description"],
        tools=list(meta["tools"]),
        write_scope=write_scope,
        bash_allow=list(meta.get("bash_allow") or []),
        output_model=OUTPUT_MODELS[meta["output_model"]],
        instructions=body,
        path=path,
    )


def load_agent_definitions(directory: Path = AGENTS_DIR) -> dict[str, AgentDefinition]:
    definitions = {}
    for name in AGENT_NAMES:
        path = directory / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"agent definition missing: {path}")
        definition = parse_agent_file(path)
        if definition.name != name:
            raise ValueError(f"{path.name}: name {definition.name!r} does not match file name")
        definitions[name] = definition
    return definitions


# ------------------------------------------------------------- request/result
@dataclass
class AgentRequest:
    agent_name: str
    prompt: str
    stage: str
    task_id: int | None = None
    replacements: dict[str, list[str]] = field(default_factory=dict)
    extra_read_dirs: list[Path] = field(default_factory=list)
    input_summary: str = ""
    # Chosen by the orchestrator (automatic selection / upgrade); None = workflow.json settings.
    model: str | None = None
    effort: str | None = None
    selection_note: str = ""


@dataclass
class BackendResult:
    structured_output: Any = None
    result_text: str = ""
    session_id: str | None = None
    num_turns: int = 0
    cost_usd: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = False
    error_subtype: str | None = None
    error_message: str | None = None
    api_error_status: int | None = None
    model: str | None = None
    assistant_error: str | None = None
    tool_calls: int = 0
    rate_limit_rejected: bool = False
    rate_limit_resets_at: float | None = None


@dataclass
class AgentResult:
    agent_name: str
    status: Status
    output: AgentOutputBase | None
    agent_run_id: int
    backend: str
    error: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str | None = None
    denials: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == Status.PASS and self.output is not None


@dataclass
class AgentContext:
    definition: AgentDefinition
    request: AgentRequest
    workspace: Path
    guard: ToolGuard
    system_prompt: str
    schema: dict[str, Any]
    max_turns: int
    budget_usd: float | None
    model: str | None
    fallback_model: str | None
    effort: str | None
    timeout_seconds: int
    emit: Callable[[str, str, dict[str, Any]], None]
    # Rate-limit signals seen during the current call; kept on the context so they survive a CLI crash.
    limit_state: dict[str, Any] = field(default_factory=dict)


def _tool_target(name: str, tool_input: dict[str, Any], workspace: Path) -> str:
    for key in ("file_path", "notebook_path", "path"):
        if tool_input.get(key):
            value = str(tool_input[key])
            try:
                return Path(value).resolve().relative_to(workspace.resolve()).as_posix()
            except ValueError:
                return value
    if name == "Bash":
        return redact(str(tool_input.get("command", "")))[:200]
    if tool_input.get("pattern"):
        return f"pattern={str(tool_input['pattern'])[:120]}"
    return ""


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


# ----------------------------------------------------------------- backends
class ClaudeAgentBackend:
    """Runs an agent with the real Claude Agent SDK (``claude_agent_sdk.query``)."""

    name = "claude"

    def __init__(self, config: AppConfig):
        self.config = config

    def cli_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in os.environ:
            upper = key.upper()
            if upper.startswith(AUTH_ENV_PREFIXES):
                continue
            if any(marker in upper for marker in SECRET_ENV_MARKERS):
                env[key] = ""  # blank out inherited secrets for the CLI and its Bash tool
        if self.config.secrets.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.config.secrets.anthropic_api_key
        wf = self.config.workflow
        if wf.rtk_enabled:  # the rewritten "rtk pytest" runs in the CLI's Bash tool with this environment
            env.update(rtk_env(DATABASE_DIR / f"rtk-recall-{self.config.project_id}.db"))
            if wf.rtk_path:
                env["PATH"] = str(Path(wf.rtk_path).parent) + os.pathsep + os.environ.get("PATH", "")
        return env

    def _sandbox(self) -> dict[str, Any] | None:
        if not self.config.workflow.sandbox_bash or sys.platform not in ("linux", "darwin"):
            return None
        return {"enabled": True, "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False, "network": {"allowLocalBinding": True}}

    async def run(self, ctx: AgentContext, prompt: str, resume_session: str | None = None) -> BackendResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            HookMatcher,
            RateLimitEvent,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            query,
        )

        tools = list(ctx.definition.tools) + ["TodoWrite"]
        fallback = ctx.fallback_model if ctx.fallback_model and ctx.fallback_model != ctx.model else None
        options = ClaudeAgentOptions(
            tools=tools,
            allowed_tools=tools + ["StructuredOutput"],
            disallowed_tools=sorted(NEVER_ALLOWED),
            system_prompt=ctx.system_prompt,
            permission_mode="dontAsk",
            cwd=str(ctx.workspace),
            add_dirs=[str(d) for d in ctx.request.extra_read_dirs],
            max_turns=ctx.max_turns,
            max_budget_usd=ctx.budget_usd,
            model=ctx.model,
            fallback_model=fallback,
            effort=ctx.effort,  # type: ignore[arg-type]
            setting_sources=[],
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[ctx.guard.pre_tool_use_hook], on_failure="block")]},  # type: ignore[list-item]
            output_format={"type": "json_schema", "schema": ctx.schema},
            env=self.cli_env(),
            stderr=lambda line: log.debug("[claude-cli] %s", redact(line.rstrip())),
            client_app="autonomous-dev/1.0",
            resume=resume_session,
            sandbox=self._sandbox(),  # type: ignore[arg-type]
        )
        result = BackendResult()
        tool_names: dict[str, str] = {}
        async with asyncio.timeout(ctx.timeout_seconds):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    if message.subtype == "init":
                        result.session_id = message.data.get("session_id")
                        result.model = message.data.get("model")
                elif isinstance(message, AssistantMessage):
                    if message.error:
                        result.assistant_error = message.error
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_names[block.id] = block.name
                            result.tool_calls += 1
                            ctx.emit("AGENT_TOOL_STARTED", f"{block.name} {_tool_target(block.name, block.input, ctx.workspace)}", {"tool": block.name, "tool_use_id": block.id})
                        elif isinstance(block, TextBlock) and block.text.strip():
                            ctx.emit("AGENT_PROGRESS", block.text.strip()[:500], {})
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            size = len(block.content) if isinstance(block.content, str) else len(json.dumps(block.content or ""))
                            name = tool_names.get(block.tool_use_id, "tool")
                            ctx.emit(
                                "AGENT_TOOL_COMPLETED",
                                f"{name} {'failed' if block.is_error else 'ok'} ({size} chars)",
                                {"tool": name, "tool_use_id": block.tool_use_id, "is_error": bool(block.is_error)},
                            )
                elif isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    if info.status == "rejected":
                        result.rate_limit_rejected = True
                        result.rate_limit_resets_at = float(info.resets_at) if info.resets_at else None
                        ctx.limit_state["rejected"] = True
                        if info.resets_at:
                            ctx.limit_state["resets_at"] = float(info.resets_at)
                    if info.status != "allowed":
                        ctx.emit(
                            "AGENT_RATE_LIMITED",
                            f"rate limit {info.status} ({info.rate_limit_type}), utilization={info.utilization}",
                            {"status": info.status, "resets_at": info.resets_at, "type": info.rate_limit_type},
                        )
                elif isinstance(message, ResultMessage):
                    result.structured_output = message.structured_output
                    result.result_text = message.result or ""
                    result.session_id = message.session_id or result.session_id
                    result.num_turns = message.num_turns
                    result.cost_usd = message.total_cost_usd
                    result.usage = message.usage or {}
                    result.duration_ms = message.duration_ms
                    result.is_error = message.is_error
                    result.error_subtype = message.subtype if message.subtype != "success" else None
                    result.api_error_status = message.api_error_status
                    if message.errors:
                        result.error_message = "; ".join(message.errors)[:2000]
        return result


@dataclass
class ScriptedCall:
    """What a scripted handler sees; file writes are checked by the same ToolGuard as real agents."""

    ctx: AgentContext
    prompt: str
    tool_calls: int = 0

    @property
    def stage(self) -> str:
        return self.ctx.request.stage

    @property
    def workspace(self) -> Path:
        return self.ctx.workspace

    def _tool(self, name: str, tool_input: dict[str, Any]) -> None:
        self.tool_calls += 1
        allowed, reason = self.ctx.guard.check(name, tool_input)
        target = _tool_target(name, tool_input, self.ctx.workspace)
        self.ctx.emit("AGENT_TOOL_STARTED", f"{name} {target}", {"tool": name, "scripted": True})
        if self.ctx.guard.on_decision:
            self.ctx.guard.on_decision(name, tool_input, allowed, reason)
        if not allowed:
            self.ctx.guard.denials.append({"tool": name, "reason": reason})
            raise PermissionError(reason)
        self.ctx.emit("AGENT_TOOL_COMPLETED", f"{name} ok", {"tool": name, "scripted": True})

    def read_file(self, rel: str) -> str:
        path = self.ctx.workspace / rel
        self._tool("Read", {"file_path": str(path)})
        return path.read_text(encoding="utf-8")

    def write_file(self, rel: str, content: str) -> None:
        path = self.ctx.workspace / rel
        self._tool("Write", {"file_path": str(path), "content": content})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def replace_in_file(self, rel: str, old: str, new: str) -> None:
        path = self.ctx.workspace / rel
        self._tool("Edit", {"file_path": str(path), "old_string": old, "new_string": new})
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError(f"{old!r} not found in {rel}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


ScriptHandler = Callable[[ScriptedCall], "dict[str, Any] | Awaitable[dict[str, Any]]"]


class ScriptedBackend:
    """Deterministic stand-in for Claude used to test the controller itself (never for real work)."""

    name = "scripted"

    def __init__(self, handlers: dict[str, ScriptHandler | list[ScriptHandler]]):
        self.handlers = handlers
        self.calls: list[tuple[str, str]] = []

    async def run(self, ctx: AgentContext, prompt: str, resume_session: str | None = None) -> BackendResult:
        handler = self.handlers.get(ctx.definition.name)
        if isinstance(handler, list):
            if not handler:
                raise AgentError(f"scripted backend has no remaining handler for {ctx.definition.name}")
            handler = handler.pop(0)
        if handler is None:
            raise AgentError(f"scripted backend has no handler for {ctx.definition.name}")
        self.calls.append((ctx.definition.name, ctx.request.stage))
        call = ScriptedCall(ctx=ctx, prompt=prompt)
        output = handler(call)
        if inspect.isawaitable(output):
            output = await output
        return BackendResult(
            structured_output=output,
            session_id=f"scripted-{ctx.definition.name}-{len(self.calls)}",
            num_turns=call.tool_calls + 1,
            cost_usd=0.0,
            tool_calls=call.tool_calls,
            model="scripted",
        )


# -------------------------------------------------------------------- runner
class AgentRunner:
    def __init__(
        self,
        config: AppConfig,
        state: StateManager,
        events: EventBus,
        backend: ClaudeAgentBackend | ScriptedBackend,
        definitions: dict[str, AgentDefinition] | None = None,
    ):
        self.config = config
        self.state = state
        self.events = events
        self.backend = backend
        self.definitions = definitions or load_agent_definitions()
        self.semaphore = asyncio.Semaphore(config.project.max_parallel_agents)
        self.run_id: int | None = None
        self.project_context = ""
        self._rtk: RtkRewriter | None = None
        self._rtk_checked = False

    async def _rtk_rewriter(self) -> RtkRewriter | None:
        """RTK rewriter when enabled and usable; checked once, the result is published as an event."""
        wf = self.config.workflow
        if not wf.rtk_enabled or self.backend.name != "claude":
            return None
        if not self._rtk_checked:
            self._rtk_checked = True
            status = await asyncio.to_thread(check_rtk, wf.rtk_path)
            if status.usable and status.path:
                self._rtk = RtkRewriter(status.path)
                self.events.publish("RTK_STATUS", f"RTK on: agents' test commands use compact output ({status.detail})", metadata={"path": status.path, "version": status.version})
            else:
                self.events.publish("RTK_STATUS", f"RTK is enabled but not usable, so agents run normal commands: {status.detail}", status="WARNING", metadata={"path": status.path})
        return self._rtk

    def _system_prompt(self, definition: AgentDefinition, write_scope: list[str], bash: list[list[str]], rtk: bool = False) -> str:
        bash_text = ", ".join(" ".join(p) for p in bash) or "none (Bash is not available to you)"
        if rtk:
            bash_text += (". Test output is shortened by RTK (failures only); if it ends with a hint such as"
                          " '[full output: rtk recall <id>]', run exactly 'rtk recall <id>' to see all of it")
        scope_text = ", ".join(write_scope) or "none - you have read-only access"
        context = f"\n\n## Project conventions (from CLAUDE.md / project docs)\n{self.project_context[:12000]}" if self.project_context else ""
        return (
            f"{definition.instructions}\n\n"
            "## Operating context (enforced by the orchestrator)\n"
            "- You run inside an autonomous pipeline. The orchestrator owns task status, git, deployments and the authoritative test runs.\n"
            "- Your working directory is the project workspace. Access outside it is blocked.\n"
            f"- Files you may write: {scope_text}\n"
            f"- Shell commands you may run: {bash_text}. No pipes, redirection, chaining or background jobs.\n"
            "- Web access, sub-agents and git are not available. Never print or write secrets.\n"
            "- A tool call denied by policy will not be allowed on retry; adapt instead of repeating it.\n"
            "- Finish with the structured result that matches the provided JSON schema. Report real outcomes only."
            f"{context}"
        )

    def _check_limits(self, request: AgentRequest) -> None:
        wf = self.config.workflow
        if self.run_id is not None:
            spent = self.state.run_cost(self.run_id)
            if spent >= wf.max_budget_usd:
                raise BudgetExceeded(f"run budget exhausted: ${spent:.2f} of ${wf.max_budget_usd:.2f}")
        if request.task_id is not None:
            used = self.state.task_turns(request.task_id)
            if used >= wf.max_agent_turns_per_task:
                raise TurnLimitExceeded(f"task {request.task_id} used {used} agent turns (limit {wf.max_agent_turns_per_task})")

    def _budget_for_call(self) -> float | None:
        wf = self.config.workflow
        per_call = wf.max_budget_usd_per_agent_call
        if self.run_id is None:
            return per_call
        remaining = wf.max_budget_usd - self.state.run_cost(self.run_id)
        return max(0.05, min(per_call, remaining))

    async def run(self, request: AgentRequest, workspace: Path) -> AgentResult:
        definition = self.definitions[request.agent_name]
        self._check_limits(request)
        settings = self.config.workflow.agent(definition.name)
        replacements = {"test_dir": [self.config.project.test_dir], **request.replacements}
        write_scope = expand_write_scope(definition.write_scope, replacements)
        write_scope = [w for w in write_scope if "{" not in w]  # unresolved placeholders grant nothing
        policy = definition.policy()

        agent_run_id = self.state.start_agent_run(
            self.config.project_id, request.task_id, definition.name, request.stage, self.backend.name,
            request.input_summary or request.prompt[:500], 1, self.run_id,
        )

        def emit(event_type: str, summary: str, metadata: dict[str, Any]) -> None:
            self.events.publish(event_type, summary, task_id=request.task_id, agent=definition.name, metadata={"agent_run_id": agent_run_id, **metadata}, source="sdk" if self.backend.name == "claude" else "scripted")
            if event_type in {"AGENT_TOOL_STARTED", "AGENT_PROGRESS"}:
                self.state.touch_agent_run(agent_run_id, 1 if event_type == "AGENT_TOOL_STARTED" else 0)

        def on_decision(tool: str, tool_input: dict[str, Any], allowed: bool, reason: str) -> None:
            if not allowed:
                target = _tool_target(tool, tool_input, workspace)
                emit("AGENT_TOOL_DENIED", f"{tool} {target}: {reason}", {"tool": tool})
                self.state.audit(f"agent:{definition.name}", "tool_denied", f"{tool} {target}", {"reason": reason, "task_id": request.task_id}, allowed=False)

        rtk = await self._rtk_rewriter() if "Bash" in definition.tools and policy.bash_prefixes() else None
        guard = ToolGuard(policy, workspace, write_scope, request.extra_read_dirs, on_decision, bash_rewriter=rtk)
        ctx = AgentContext(
            definition=definition,
            request=request,
            workspace=workspace,
            guard=guard,
            system_prompt=self._system_prompt(definition, write_scope, policy.bash_prefixes(), rtk is not None),
            schema=inline_schema(definition.output_model),
            max_turns=settings.max_turns or self.config.workflow.default_max_turns,
            budget_usd=self._budget_for_call(),
            model=request.model or settings.model or self.config.workflow.model,
            fallback_model=self.config.workflow.fallback_model,
            effort=request.effort or settings.effort or self.config.workflow.default_effort,
            timeout_seconds=settings.timeout_seconds or self.config.workflow.agent_timeout_seconds,
            emit=emit,
        )
        choice = f"{ctx.model or 'default model'} / {ctx.effort or 'default'} effort" + (f" - {request.selection_note}" if request.selection_note else "")
        self.events.publish("AGENT_STARTED", f"{definition.name} started ({request.stage}): {choice}", status="IN_PROGRESS", task_id=request.task_id, agent=definition.name,
                            metadata={"agent_run_id": agent_run_id, "backend": self.backend.name, "model": ctx.model, "effort": ctx.effort, "selection": request.selection_note})

        async with self.semaphore:
            try:
                backend_result, output = await self._run_with_retries(ctx, request.prompt)
            except BaseException as exc:
                message = redact(f"{type(exc).__name__}: {exc}")[:2000]
                self.state.finish_agent_run(agent_run_id, status="ERROR", error_message=message)
                self.events.publish("AGENT_FAILED", f"{definition.name} failed: {message[:300]}", status="ERROR", task_id=request.task_id, agent=definition.name, metadata={"agent_run_id": agent_run_id})
                if isinstance(exc, (FatalAgentError, BudgetExceeded, TurnLimitExceeded, UsageLimitReached)) or not isinstance(exc, Exception):
                    raise
                return AgentResult(definition.name, Status.ERROR, None, agent_run_id, self.backend.name, error=message, denials=guard.denials)

        usage = backend_result.usage or {}
        status = output.status if output else Status.ERROR
        self.state.finish_agent_run(
            agent_run_id,
            status=status.value,
            output_summary=redact(output.summary)[:4000] if output else None,
            output_json=json.loads(output.model_dump_json()) if output else None,
            error_message=None if output else "no valid structured output",
            session_id=backend_result.session_id,
            model=backend_result.model,
            num_turns=backend_result.num_turns,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
            cost_usd=backend_result.cost_usd,
            duration_ms=backend_result.duration_ms,
        )
        if self.run_id is not None and backend_result.cost_usd:
            self.state.increment_run(self.run_id, "cost_usd", backend_result.cost_usd)
        event_type = "AGENT_COMPLETED" if status != Status.ERROR else "AGENT_FAILED"
        self.events.publish(
            event_type,
            f"{definition.name}: {status.value} - {redact(output.summary)[:300] if output else 'no valid output'}",
            status=status.value,
            task_id=request.task_id,
            agent=definition.name,
            metadata={"agent_run_id": agent_run_id, "turns": backend_result.num_turns, "cost_usd": backend_result.cost_usd, "denied_tool_calls": len(guard.denials)},
        )
        return AgentResult(
            definition.name, status, output, agent_run_id, self.backend.name,
            error=None if output else "invalid structured output", cost_usd=backend_result.cost_usd or 0.0,
            num_turns=backend_result.num_turns, session_id=backend_result.session_id, denials=guard.denials,
        )

    async def _invoke(self, ctx: AgentContext, prompt: str, resume: str | None) -> BackendResult:
        from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ClaudeSDKError, ProcessError

        ctx.limit_state.clear()
        try:
            result = await self.backend.run(ctx, prompt, resume)
        except CLINotFoundError as exc:
            raise FatalAgentError(f"Claude Code CLI not found: {exc}") from exc
        except TimeoutError as exc:
            raise AgentError(f"agent timed out after {ctx.timeout_seconds}s", retryable=False) from exc
        except (CLIConnectionError, ProcessError) as exc:
            detail = f"{exc} {getattr(exc, 'stderr', '') or ''}"
            self._raise_if_usage_limit(ctx, detail, exc)
            text = str(exc).lower()
            if "auth" in text or "login" in text or "api key" in text:
                raise FatalAgentError(f"Claude authentication problem: {exc}") from exc
            raise AgentError(f"SDK process error: {exc}", retryable=True) from exc
        except ClaudeSDKError as exc:
            self._raise_if_usage_limit(ctx, str(exc), exc)
            raise AgentError(f"SDK error: {exc}", retryable=True) from exc
        if result.is_error:
            self._raise_if_usage_limit(ctx, f"{result.error_message or ''} {result.result_text or ''}", None)
        if result.assistant_error in {"authentication_failed", "billing_error"}:
            raise FatalAgentError(f"Claude API {result.assistant_error}: check authentication and billing")
        if result.error_subtype == "error_max_budget_usd":
            raise BudgetExceeded("agent call stopped by max_budget_usd")
        if result.rate_limit_rejected and (result.is_error or result.structured_output is None):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(result.rate_limit_resets_at)) if result.rate_limit_resets_at else "unknown"
            raise UsageLimitReached(f"Claude usage limit reached (resets at {when})", result.rate_limit_resets_at)
        if result.is_error and (result.api_error_status in RETRYABLE_HTTP or result.assistant_error in {"rate_limit", "server_error"}):
            error = AgentError(f"transient API error {result.api_error_status or result.assistant_error}", retryable=True)
            error.rate_limited = result.api_error_status == 429 or result.assistant_error == "rate_limit"  # type: ignore[attr-defined]
            raise error
        return result

    @staticmethod
    def _raise_if_usage_limit(ctx: AgentContext, text: str, cause: BaseException | None) -> None:
        """Turn a failed call into UsageLimitReached when the plan limit caused it."""
        if not (ctx.limit_state.get("rejected") or is_usage_limit_text(text)):
            return
        resets_at = ctx.limit_state.get("resets_at") or parse_reset_time(text)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(resets_at)) if resets_at else "unknown"
        match = USAGE_LIMIT_RE.search(text or "")
        what = match.group(0) if match else "usage limit"
        error = UsageLimitReached(f"Claude usage limit reached ({what}; resets at {when})", resets_at)
        if cause is not None:
            raise error from cause
        raise error

    def _validate(self, ctx: AgentContext, result: BackendResult) -> tuple[AgentOutputBase | None, str]:
        data = result.structured_output
        if data is None:
            data = _extract_json(result.result_text)
        if data is None:
            reason = result.error_subtype or result.error_message or "no structured output returned"
            return None, reason
        try:
            return ctx.definition.output_model.model_validate(data), ""
        except ValidationError as exc:
            return None, str(exc)[:3000]

    async def _run_with_retries(self, ctx: AgentContext, prompt: str) -> tuple[BackendResult, AgentOutputBase | None]:
        wf = self.config.workflow
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await self._invoke(ctx, prompt, None)
                break
            except AgentError as exc:
                if exc.retryable and attempt > wf.transient_retry_attempts and getattr(exc, "rate_limited", False):
                    raise UsageLimitReached(f"Claude rate/usage limit persisted after {attempt - 1} retries", None) from exc
                if not exc.retryable or isinstance(exc, FatalAgentError) or attempt > wf.transient_retry_attempts:
                    raise
                delay = min(wf.backoff_max_seconds, wf.backoff_base_seconds * 2 ** (attempt - 1)) * (0.8 + random.random() * 0.4)
                ctx.emit("AGENT_PROGRESS", f"transient failure ({exc}); retry {attempt}/{wf.transient_retry_attempts} in {delay:.0f}s", {"retry": attempt})
                await asyncio.sleep(delay)
        output, problem = self._validate(ctx, result)
        corrections = 0
        while output is None and corrections < wf.max_invalid_output_retries and result.session_id and not result.error_subtype == "error_max_turns":
            corrections += 1
            ctx.emit("AGENT_PROGRESS", f"structured output invalid, requesting correction ({problem[:200]})", {"correction": corrections})
            correction_prompt = (
                "Your final structured result was missing or did not validate against the required JSON schema:\n"
                f"{problem}\nReturn the corrected structured result now. Do not repeat any work."
            )
            previous_turns, previous_cost = result.num_turns, result.cost_usd or 0.0
            result = await self._invoke(ctx, correction_prompt, result.session_id)
            result.num_turns += previous_turns
            result.cost_usd = (result.cost_usd or 0.0) + previous_cost
            output, problem = self._validate(ctx, result)
        if output is None:
            log.warning("agent %s produced no valid structured output: %s", ctx.definition.name, problem[:500])
        return result, output
