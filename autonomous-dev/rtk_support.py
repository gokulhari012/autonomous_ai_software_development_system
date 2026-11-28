"""Optional RTK (Rust Token Killer) support for the agents' shell commands.

RTK (https://github.com/rtk-ai/rtk) is a separate program that runs a command and
returns a compact version of its output, for example only the failing tests from
pytest, so agents spend fewer tokens reading it. It is off unless
``workflow.json`` has ``"rtk_enabled": true``.

Agents run with ``setting_sources=[]``, so a global ``rtk init -g`` hook in
``~/.claude/settings.json`` never reaches them. Instead, the controller's own
``PreToolUse`` hook (:class:`permissions.ToolGuard`) first checks the *original*
command against the agent's allowlist and only then asks ``rtk rewrite`` for a
compact form. The rewrite is used only when it is ``rtk pytest`` with arguments
taken from the original command, so RTK can never widen what an agent may run.
Anything unexpected (RTK missing, wrong program, timeout, no rewrite) falls back
to the original command.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("rtk")

MIN_VERSION = (0, 25, 0)  # first release with "rtk rewrite"
REWRITE_TIMEOUT_SECONDS = 3.0
# RTK subcommands the controller accepts as a rewrite. Each must match a command
# that the permission guard already allows (permissions.AGENT_POLICIES_DEFAULT_BASH).
SAFE_SUBCOMMANDS = {"pytest"}
# "rtk recall <id>" prints the full output that RTK shortened.
RECALL_RE = re.compile(r"^rtk(?:\.exe)?\s+recall\s+[A-Za-z0-9_-]{4,64}$")
VERSION_RE = re.compile(r"\brtk\s+v?(\d+)\.(\d+)\.(\d+)")
INSTALL_HINT = "install it with 'winget install rtk-ai.rtk' (Windows) or see https://github.com/rtk-ai/rtk"


@dataclass
class RtkStatus:
    usable: bool
    path: str | None
    version: str | None
    detail: str


def rtk_env(recall_db: Path | None = None) -> dict[str, str]:
    """Environment entries for every RTK call: no telemetry, recall stored per project."""
    env = {"RTK_TELEMETRY_DISABLED": "1"}
    if recall_db is not None:
        env["RTK_RECALL_DB"] = str(recall_db)
    return env


def find_rtk(configured: str | None) -> str | None:
    if configured and configured.strip():
        path = Path(configured.strip()).expanduser()
        return str(path) if path.is_file() else None
    return shutil.which("rtk")


def run_rtk(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env={**os.environ, **rtk_env()},
                          stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")


def check_rtk(configured: str | None) -> RtkStatus:
    """Find RTK and confirm it is the Rust Token Killer and can rewrite pytest commands."""
    path = find_rtk(configured)
    if not path:
        where = f"not found at {configured.strip()}" if configured and configured.strip() else "not found on PATH"
        return RtkStatus(False, None, None, f"rtk {where} - {INSTALL_HINT}")
    try:
        out = run_rtk([path, "--version"], 10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RtkStatus(False, path, None, f"could not run {path}: {exc}")
    match = VERSION_RE.search(out.stdout + out.stderr)
    if not match:
        return RtkStatus(False, path, None, f"{path} did not report an rtk version; it may be a different program named rtk")
    version = ".".join(match.groups())
    if tuple(int(p) for p in match.groups()) < MIN_VERSION:
        return RtkStatus(False, path, version, f"rtk {version} is too old (need {'.'.join(map(str, MIN_VERSION))} or later for 'rtk rewrite')")
    try:
        probe = run_rtk([path, "rewrite", "--", "python -m pytest -q"], 10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RtkStatus(False, path, version, f"'rtk rewrite' failed: {exc}")
    if probe.returncode not in (0, 3) or not probe.stdout.strip().startswith("rtk pytest"):
        return RtkStatus(False, path, version, f"'rtk rewrite' did not rewrite pytest (exit {probe.returncode}); "
                                               "this may be the unrelated 'Rust Type Kit' - reinstall from rtk-ai")
    return RtkStatus(True, path, version, f"rtk {version} at {path}")


def accept_rewrite(original: str, rewritten: str) -> str | None:
    """The rewritten command if it is safe to run in place of ``original``, else None.

    Safe means: ``rtk <subcommand> ...`` with a subcommand from SAFE_SUBCOMMANDS and every
    argument already present in the original command (which the guard has checked)."""
    rewritten = rewritten.strip()
    if not rewritten or rewritten == original.strip():
        return None
    from permissions import SHELL_METACHARACTERS

    if SHELL_METACHARACTERS.search(rewritten):
        return None
    try:
        new_tokens = shlex.split(rewritten, posix=True)
        old_tokens = shlex.split(original, posix=True)
    except ValueError:
        return None
    if len(new_tokens) < 2 or new_tokens[0] != "rtk" or new_tokens[1] not in SAFE_SUBCOMMANDS:
        return None
    available = Counter(old_tokens)
    for token in new_tokens[2:]:
        if available[token] <= 0:
            return None
        available[token] -= 1
    return rewritten


class RtkRewriter:
    """Async callable used by the permission hook: command -> compact command or None."""

    def __init__(self, path: str):
        self.path = path
        self.env = {**os.environ, **rtk_env()}

    async def __call__(self, command: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.path, "rewrite", "--", command, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self.env)
        except OSError as exc:
            log.warning("rtk could not be started: %s", exc)
            return None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), REWRITE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            log.warning("rtk rewrite timed out; running the original command")
            return None
        if process.returncode not in (0, 3):
            return None
        return accept_rewrite(command, stdout.decode("utf-8", "replace"))
