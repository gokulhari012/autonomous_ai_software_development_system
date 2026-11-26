"""Safe command execution for the orchestrator.

Every external command the controller runs goes through :class:`SafeExecutor`:

* ``shell=False`` always - arguments are never interpreted by a shell.
* Structural allowlist per executable (git, python -m <module>, semgrep, docker
  compose, registered application executables). Destructive git operations
  (push, reset, clean, rebase, force flags, branch deletion, amend) are rejected.
* The working directory must be inside an allowed root (project workspace,
  controller reports directory).
* A scrubbed environment: only a fixed set of OS variables is inherited, so
  tokens and API keys present in the controller's environment never reach
  test runs or the application under test.
* Timeouts that kill the whole process tree, bounded output capture, and an
  audit-log entry for every command (allowed or rejected).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from logging_setup import redact

log = logging.getLogger("safe_exec")

MAX_CAPTURE_BYTES = 2_000_000

INHERITED_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USER", "LOGNAME", "LANG",
    "LC_ALL", "LC_CTYPE", "TERM", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
    "SESSIONNAME", "PLAYWRIGHT_BROWSERS_PATH", "DOCKER_HOST", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "VIRTUAL_ENV",
}

ALLOWED_PYTHON_MODULES = {"pytest", "bandit", "pip_audit", "flask", "compileall", "pip"}
PIP_READ_ONLY = {"show", "list", "freeze", "--version"}

GIT_ALLOWED_SUBCOMMANDS = {
    "status", "rev-parse", "branch", "log", "diff", "add", "commit", "worktree", "show", "init",
    "ls-files", "symbolic-ref", "merge-base", "checkout", "switch", "config", "cat-file", "version",
}
GIT_FORBIDDEN_ARGS = {
    "--force", "-f", "--hard", "--force-with-lease", "--mirror", "--amend", "--no-verify", "--delete",
    "-d", "-D", "-m", "-M", "--move", "--orphan", "--prune", "-B", "--detach", "--discard-changes",
    "--merge", "--ours", "--theirs", "--patch", "-p", "--exec", "--upload-pack", "--receive-pack",
}
SEMGREP_ALLOWED = {"scan", "--version"}
DOCKER_COMPOSE_ALLOWED = {"up", "down", "ps", "logs", "config", "build", "version"}
DOCKER_ALLOWED = {"info", "version", "compose"}


class CommandRejected(Exception):
    """The command violates the execution policy."""


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    def summary(self, limit: int = 1500) -> str:
        text = (self.stdout + "\n" + self.stderr).strip()
        return redact(text[-limit:])


@dataclass
class CommandPolicy:
    allowed_roots: list[Path]
    python_executables: list[str] = field(default_factory=list)
    app_executables: list[str] = field(default_factory=list)
    app_modules: set[str] = field(default_factory=set)
    app_scripts: list[str] = field(default_factory=list)
    allow_pyinstaller: bool = False

    def _within_roots(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self.allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def _is_python(self, exe: str) -> bool:
        name = Path(exe).name.lower()
        if exe in self.python_executables:
            return True
        return name in {"python", "python.exe", "python3", "python3.exe"} and not os.path.isabs(exe)

    def validate(self, argv: list[str], cwd: Path) -> str:
        """Validate a command. Returns the command family on success, raises CommandRejected otherwise."""
        if not argv or not all(isinstance(a, str) for a in argv):
            raise CommandRejected("argv must be a non-empty list of strings")
        for arg in argv:
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise CommandRejected("arguments may not contain NUL or newline characters")
        if not self._within_roots(cwd):
            raise CommandRejected(f"working directory {cwd} is outside the permitted workspace")

        exe = argv[0]
        exe_name = Path(exe).name.lower().removesuffix(".exe")

        if exe_name == "git":
            self._validate_git(argv[1:])
            return "git"
        if self._is_python(exe):
            self._validate_python(argv[1:], cwd)
            return "python"
        if exe_name == "semgrep":
            if not argv[1:] or argv[1] not in SEMGREP_ALLOWED:
                raise CommandRejected("only 'semgrep scan' and 'semgrep --version' are permitted")
            return "semgrep"
        if exe_name == "docker":
            self._validate_docker(argv[1:])
            return "docker"
        if exe in self.app_executables:
            if not self._within_roots(Path(exe)):
                raise CommandRejected("application executable must be inside the project workspace")
            return "app"
        raise CommandRejected(f"executable {exe!r} is not on the allowlist")

    def _validate_git(self, args: list[str]) -> None:
        i = 0
        # Global options: -c key=value (identity only) and -C <path>
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-c" and i + 1 < len(args):
                key = args[i + 1].split("=", 1)[0]
                if key not in {"user.name", "user.email", "core.quotepath", "color.ui", "core.autocrlf"}:
                    raise CommandRejected(f"git -c {key} is not permitted")
                i += 2
            elif args[i] == "-C" and i + 1 < len(args):
                if not self._within_roots(Path(args[i + 1])):
                    raise CommandRejected("git -C path is outside the permitted workspace")
                i += 2
            elif args[i] in {"--no-pager", "--version"}:
                i += 1
            else:
                raise CommandRejected(f"git global option {args[i]} is not permitted")
        if i >= len(args):
            return
        sub, rest = args[i], args[i + 1 :]
        if sub not in GIT_ALLOWED_SUBCOMMANDS:
            raise CommandRejected(f"git {sub} is not permitted")
        # Values following -m (commit messages) are not flags; skip them.
        flags: list[str] = []
        skip_next = False
        for a in rest:
            if skip_next:
                skip_next = False
                continue
            if sub == "commit" and a == "-m":
                skip_next = True
                continue
            flags.append(a)
        bad = [a for a in flags if a in GIT_FORBIDDEN_ARGS or a.startswith("--force")]
        if bad and not (sub in {"log", "show", "diff"} and set(bad) <= {"-p", "--patch", "-M"}):
            raise CommandRejected(f"git {sub} with {bad} is not permitted")
        if sub == "worktree" and (not rest or rest[0] not in {"add", "list"}):
            raise CommandRejected("only 'git worktree add/list' are permitted")
        if sub == "config" and (not rest or rest[0] not in {"--get", "--list", "-l"}):
            raise CommandRejected("only read-only git config is permitted")
        if sub == "checkout":
            if "--" in rest or "." in rest:
                raise CommandRejected("git checkout of paths is not permitted (it can discard changes)")
        if sub == "branch":
            allowed_flags = {"--show-current", "--list", "-a", "--all", "--format", "-v", "--no-color"}
            for a in rest:
                if a.startswith("-") and a.split("=")[0] not in allowed_flags:
                    raise CommandRejected(f"git branch {a} is not permitted")
        if sub == "add":
            for a in rest:
                if Path(a).name.lower() in {".env", ".env.local", "credentials.json", "id_rsa"}:
                    raise CommandRejected(f"refusing to stage secret file {a}")

    def _validate_python(self, args: list[str], cwd: Path) -> None:
        if not args:
            raise CommandRejected("interactive python is not permitted")
        if args[0] in {"-c", "-i"}:
            raise CommandRejected("inline python code is not permitted")
        if args[0] in {"--version", "-V"}:
            return
        if args[0] == "-m":
            if len(args) < 2:
                raise CommandRejected("python -m requires a module")
            module = args[1]
            if module == "pip":
                if len(args) < 3 or args[2] not in PIP_READ_ONLY:
                    raise CommandRejected("only read-only pip commands (show/list/freeze) are permitted")
                return
            if module == "PyInstaller" and self.allow_pyinstaller:
                return
            if module in ALLOWED_PYTHON_MODULES or module in self.app_modules:
                return
            raise CommandRejected(f"python -m {module} is not permitted")
        script = Path(args[0])
        if not script.is_absolute():
            script = cwd / script
        if str(script.resolve()) in {str(Path(s).resolve()) for s in self.app_scripts} and self._within_roots(script):
            return
        raise CommandRejected(f"python script {args[0]} is not a registered application entry point")

    def _validate_docker(self, args: list[str]) -> None:
        if not args or args[0] not in DOCKER_ALLOWED:
            raise CommandRejected("only 'docker info/version/compose' are permitted")
        if args[0] == "compose":
            subs = [a for a in args[1:] if not a.startswith("-")]
            # skip values of -f/--file and -p/--project-name
            cleaned: list[str] = []
            skip = False
            for a in args[1:]:
                if skip:
                    skip = False
                    continue
                if a in {"-f", "--file", "-p", "--project-name", "--env-file"}:
                    skip = True
                    continue
                if not a.startswith("-"):
                    cleaned.append(a)
            subs = cleaned
            if not subs or subs[0] not in DOCKER_COMPOSE_ALLOWED:
                raise CommandRejected(f"docker compose {subs[:1]} is not permitted")
            if "--privileged" in args or "-v" in args and subs[0] == "down":
                raise CommandRejected("docker compose with --privileged or volume removal is not permitted")


def base_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in INHERITED_ENV_KEYS}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def kill_process_tree(pid: int, timeout: float = 10.0) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for proc in [*children, parent]:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs([*children, parent], timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def find_executable(name: str) -> str | None:
    """Locate a tool, preferring the controller's own virtual environment."""
    scripts_dir = Path(sys.executable).parent
    for candidate in (scripts_dir / name, scripts_dir / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


class SafeExecutor:
    def __init__(self, policy: CommandPolicy, state: Any | None = None, actor: str = "orchestrator"):
        self.policy = policy
        self.state = state
        self.actor = actor

    def _audit(self, action: str, argv: list[str], cwd: Path, allowed: bool, detail: dict[str, Any]) -> None:
        line = redact(" ".join(argv))
        if allowed:
            log.debug("exec %s (cwd=%s)", line, cwd)
        else:
            log.warning("REJECTED command %s (cwd=%s): %s", line, cwd, detail.get("reason"))
        if self.state is not None:
            try:
                self.state.audit(self.actor, action, line, {"cwd": str(cwd), **detail}, allowed)
            except Exception:  # auditing must never break execution
                log.exception("failed to write audit log")

    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: float = 600,
        env_extra: dict[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> CommandResult:
        cwd = Path(cwd)
        try:
            family = self.policy.validate(argv, cwd)
        except CommandRejected as exc:
            self._audit("command_rejected", argv, cwd, False, {"reason": str(exc)})
            raise
        start = time.monotonic()
        result = CommandResult(argv=argv, cwd=str(cwd), exit_code=None)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=base_environment(env_extra),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            result.error = f"failed to start {argv[0]}: {exc}"
            result.duration_seconds = time.monotonic() - start
            self._audit("command", argv, cwd, True, {"family": family, "error": result.error})
            return result
        try:
            out, err = proc.communicate(input=stdin_text.encode() if stdin_text else None, timeout=timeout)
            result.exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            out, err = proc.communicate()
            result.timed_out = True
            result.error = f"timed out after {timeout:.0f}s"
        result.stdout = out[-MAX_CAPTURE_BYTES:].decode("utf-8", errors="replace")
        result.stderr = err[-MAX_CAPTURE_BYTES:].decode("utf-8", errors="replace")
        result.duration_seconds = round(time.monotonic() - start, 2)
        self._audit(
            "command",
            argv,
            cwd,
            True,
            {"family": family, "exit_code": result.exit_code, "duration": result.duration_seconds, "timed_out": result.timed_out},
        )
        return result

    def spawn(self, argv: list[str], cwd: Path, log_path: Path, env_extra: dict[str, str] | None = None) -> subprocess.Popen:
        """Start a long-running process (staging server, desktop app) with output to ``log_path``."""
        cwd = Path(cwd)
        try:
            family = self.policy.validate(argv, cwd)
        except CommandRejected as exc:
            self._audit("command_rejected", argv, cwd, False, {"reason": str(exc)})
            raise
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")  # noqa: SIM115 - owned by the child process lifetime
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=base_environment(env_extra),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            shell=False,
            **kwargs,
        )
        handle.close()  # the child keeps its own handle
        self._audit("spawn", argv, cwd, True, {"family": family, "pid": proc.pid, "log": str(log_path)})
        return proc
