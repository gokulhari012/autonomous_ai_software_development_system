"""Git management with strict safety rules.

Rules enforced here (and again by the command policy in ``safe_exec``):

* never force push, never push at all (pushing is a human decision);
* never commit to the main/default branch;
* never delete branches, reset, clean, rebase, amend or check out paths;
* never overwrite or discard uncommitted user changes.

By default the controller works in a dedicated **git worktree** on the
development branch. The user's own working tree - including any uncommitted
changes - is never touched.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from safe_exec import CommandResult, SafeExecutor, find_executable

log = logging.getLogger("git")

SECRET_FILE_NAMES = {".env", ".env.local", ".env.production", "credentials.json", "id_rsa", "id_ed25519"}
SKIP_BACKUP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
TASK_TRAILER = "Autonomous-Task"


class GitError(Exception):
    pass


class GitSafetyError(GitError):
    """The requested operation would put user work at risk; human action is required."""


@dataclass
class WorkspaceInfo:
    workspace_path: Path
    repo_path: Path
    dev_branch: str
    base_branch: str | None
    base_commit: str | None
    isolation: str
    user_branch: str | None = None
    user_uncommitted_changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class GitManager:
    def __init__(self, executor: SafeExecutor, author_name: str, author_email: str):
        self.executor = executor
        self.git = find_executable("git") or "git"
        self.author_name = author_name
        self.author_email = author_email

    # ------------------------------------------------------------ primitives
    def run(self, args: list[str], cwd: Path, check: bool = True, timeout: float = 120) -> CommandResult:
        argv = [self.git, "-c", "core.quotepath=false", "-c", "color.ui=never", *args]
        result = self.executor.run(argv, cwd, timeout=timeout)
        if check and not result.ok:
            raise GitError(f"git {' '.join(args[:3])} failed ({result.exit_code}): {result.summary(800)}")
        return result

    def is_repo(self, path: Path) -> bool:
        if not path.exists():
            return False
        result = self.run(["rev-parse", "--is-inside-work-tree"], path, check=False)
        return result.ok and result.stdout.strip() == "true"

    def is_own_repo(self, path: Path) -> bool:
        """True only if ``path`` is the root of its own repository (not merely inside a parent repo)."""
        if not self.is_repo(path):
            return False
        return self.repo_root(path).resolve() == path.resolve()

    def repo_root(self, path: Path) -> Path:
        return Path(self.run(["rev-parse", "--show-toplevel"], path).stdout.strip())

    def git_dir(self, path: Path) -> Path:
        value = self.run(["rev-parse", "--git-dir"], path).stdout.strip()
        git_dir = Path(value)
        return git_dir if git_dir.is_absolute() else (path / git_dir)

    def has_commits(self, path: Path) -> bool:
        return self.run(["rev-parse", "--verify", "--quiet", "HEAD"], path, check=False).ok

    def head_commit(self, path: Path) -> str | None:
        result = self.run(["rev-parse", "HEAD"], path, check=False)
        return result.stdout.strip() if result.ok else None

    def current_branch(self, path: Path) -> str | None:
        result = self.run(["branch", "--show-current"], path, check=False)
        name = result.stdout.strip()
        return name or None

    def branch_exists(self, path: Path, name: str) -> bool:
        return self.run(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], path, check=False).ok

    def status_porcelain(self, path: Path) -> list[tuple[str, str]]:
        result = self.run(["status", "--porcelain=v1", "--untracked-files=all"], path)
        entries = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            code, file_path = line[:2], line[3:]
            if " -> " in file_path:
                file_path = file_path.split(" -> ", 1)[1]
            entries.append((code, file_path.strip().strip('"')))
        return entries

    def uncommitted_changes(self, path: Path) -> list[str]:
        return [p for _, p in self.status_porcelain(path)]

    def in_progress_operation(self, path: Path) -> str | None:
        git_dir = self.git_dir(path)
        markers = {
            "MERGE_HEAD": "merge",
            "rebase-merge": "rebase",
            "rebase-apply": "rebase/am",
            "CHERRY_PICK_HEAD": "cherry-pick",
            "REVERT_HEAD": "revert",
            "BISECT_LOG": "bisect",
        }
        for marker, name in markers.items():
            if (git_dir / marker).exists():
                return name
        return None

    def list_worktrees(self, repo: Path) -> list[dict[str, str]]:
        result = self.run(["worktree", "list", "--porcelain"], repo)
        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    worktrees.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            worktrees.append(current)
        return worktrees

    def log(self, path: Path, limit: int = 30) -> list[dict[str, str]]:
        if not self.has_commits(path):
            return []
        result = self.run(["log", f"-{limit}", "--format=%H%x1f%an%x1f%aI%x1f%s"], path)
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
        return commits

    def diff(self, path: Path, base: str | None, stat: bool = False, max_chars: int = 120_000) -> str:
        """Diff of the working tree (tracked changes) against ``base`` plus a list of untracked files."""
        args = ["diff", "--no-color"]
        if stat:
            args.append("--stat")
        if base:
            args.append(base)
        text = self.run(args, path, check=False).stdout
        untracked = [p for code, p in self.status_porcelain(path) if code == "??"]
        if untracked and not stat:
            parts = [text]
            for rel in untracked:
                file_path = path / rel
                try:
                    if file_path.stat().st_size > 200_000:
                        parts.append(f"\n--- new file {rel} (large file omitted)\n")
                        continue
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if Path(rel).name.lower() in SECRET_FILE_NAMES:
                    continue
                parts.append(f"\n--- /dev/null\n+++ b/{rel} (new file)\n" + "\n".join("+" + l for l in content.splitlines()))
            text = "".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [diff truncated at {max_chars} characters]"
        return text

    def changed_files(self, path: Path, base: str | None) -> list[str]:
        files: set[str] = set()
        if base:
            result = self.run(["diff", "--name-only", base], path, check=False)
            files.update(l.strip() for l in result.stdout.splitlines() if l.strip())
        files.update(self.uncommitted_changes(path))
        return sorted(files)

    def find_task_commit(self, path: Path, task_id: int) -> str | None:
        if not self.has_commits(path):
            return None
        result = self.run(["log", "-50", f"--grep={TASK_TRAILER}: {task_id}$", "--format=%H"], path, check=False)
        sha = result.stdout.strip().splitlines()
        return sha[0] if sha else None

    # --------------------------------------------------------------- remotes
    def remote_url(self, path: Path, remote: str) -> str | None:
        result = self.run(["remote", "get-url", remote], path, check=False)
        return result.stdout.strip() if result.ok else None

    def ensure_remote(self, path: Path, remote: str, url: str) -> str:
        """Make sure ``remote`` exists (adding it from ``url`` if needed). Returns a short status message."""
        current = self.remote_url(path, remote)
        if current:
            if url and current != url:
                return f"remote {remote!r} already points to {current}; left unchanged"
            return f"remote {remote!r}: {current}"
        if not url:
            raise GitError(f"remote {remote!r} does not exist and no repository URL is configured")
        self.run(["remote", "add", remote, url], path)
        return f"added remote {remote!r} -> {url}"

    def check_remote(self, path: Path, remote: str) -> tuple[bool, str]:
        """Read-only connectivity/credentials check (git ls-remote)."""
        result = self.executor.run([self.git, "ls-remote", "--heads", remote], path, timeout=60, env_extra={"GIT_TERMINAL_PROMPT": "0"})
        return result.ok, (result.summary(600) or "ok")

    def push(self, path: Path, remote: str, branch: str) -> tuple[bool, str]:
        """Push the development branch. Never forces; a rejected push is reported, not overridden."""
        result = self.executor.run(
            [self.git, "push", "--set-upstream", remote, branch], path, timeout=300,
            env_extra={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        )
        if result.ok:
            return True, f"pushed {branch} to {remote}"
        text = result.summary(800)
        if "rejected" in text or "non-fast-forward" in text:
            text = "push rejected (the remote branch has commits this workspace does not have); resolve it manually - the controller never force-pushes. " + text
        elif "Authentication" in text or "could not read Username" in text or "Permission denied" in text:
            text = "authentication failed: log in with your git credential manager or SSH key so `git push` works without a prompt. " + text
        return False, text

    # -------------------------------------------------------------- commits
    def commit_changes(self, path: Path, message: str, task_id: int | None, protected_branches: set[str], body: list[str] | None = None) -> str | None:
        """Stage every non-secret change in the workspace and commit it. Returns the new commit sha."""
        branch = self.current_branch(path)
        if branch is None and self.has_commits(path):
            raise GitSafetyError("refusing to commit on a detached HEAD")
        if branch in protected_branches:
            raise GitSafetyError(f"refusing to commit directly to protected branch {branch!r}")
        changes = [p for p in self.uncommitted_changes(path) if Path(p).name.lower() not in SECRET_FILE_NAMES]
        if not changes:
            return None
        for start in range(0, len(changes), 50):
            self.run(["add", "--", *changes[start : start + 50]], path)
        args = ["-c", f"user.name={self.author_name}", "-c", f"user.email={self.author_email}", "commit", "-m", message.replace("\n", " ")[:200]]
        for paragraph in body or []:
            text = " ".join(paragraph.split())[:2000]  # one paragraph per -m; arguments may not contain newlines
            if text:
                args += ["-m", text]
        if task_id is not None:
            args += ["-m", f"{TASK_TRAILER}: {task_id}"]
        result = self.executor.run([self.git, *args], path, timeout=120)
        if not result.ok:
            raise GitError(f"git commit failed: {result.summary(800)}")
        return self.head_commit(path)

    # ---------------------------------------------------- workspace setup
    def backup_directory(self, source: Path, backup_dir: Path) -> Path:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = backup_dir / f"{source.name}-{stamp}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(source):
                dirs[:] = [d for d in dirs if d not in SKIP_BACKUP_DIRS]
                for name in files:
                    full = Path(root) / name
                    zf.write(full, full.relative_to(source))
        return archive

    def prepare_workspace(
        self,
        project_dir: Path,
        dev_branch: str,
        default_branch: str,
        isolation: str,
        worktree_path: Path,
        mode: str,
        allow_git_init: bool,
        backup_dir: Path,
    ) -> WorkspaceInfo:
        protected = {default_branch, "main", "master"}
        notes: list[str] = []

        if not project_dir.exists():
            if mode != "new":
                raise GitError(f"project directory {project_dir} does not exist")
            project_dir.mkdir(parents=True)
            notes.append(f"Created new project directory {project_dir}.")

        if not self.is_own_repo(project_dir):
            visible = [p for p in project_dir.iterdir() if p.name not in {".DS_Store"}]
            if mode != "new" and self.is_repo(project_dir):
                raise GitSafetyError(
                    f"{project_dir} is a sub-folder of another Git repository ({self.repo_root(project_dir)}). "
                    "Point the project at the repository root, or move the project into its own folder."
                )
            if visible and not (mode == "new" or allow_git_init):
                raise GitSafetyError(
                    f"{project_dir} contains files but is not a Git repository. The controller will not modify "
                    "an unversioned project automatically. Either initialise Git yourself, or set "
                    "\"allow_git_init\": true in project.json; the controller will then back up the directory "
                    "to the backups/ folder, initialise Git on the development branch and commit a baseline."
                )
            self.run(["init", "-b", dev_branch], project_dir)
            notes.append(f"Initialised a Git repository on branch {dev_branch!r} (no commit was made to {default_branch!r}).")
            if visible:
                archive = self.backup_directory(project_dir, backup_dir)
                notes.append(f"Backed up existing files to {archive} before creating the baseline.")
                sha = self.commit_changes(project_dir, "chore: baseline snapshot of existing files", None, protected)
                notes.append(f"Baseline commit {sha[:10] if sha else '(nothing to commit)'} created on {dev_branch!r}.")
            return WorkspaceInfo(project_dir, project_dir, dev_branch, None, self.head_commit(project_dir), "in-place", dev_branch, [], notes)

        repo = self.repo_root(project_dir)
        operation = self.in_progress_operation(repo)
        if operation:
            raise GitSafetyError(f"a git {operation} is in progress in {repo}; finish or abort it manually first")
        user_branch = self.current_branch(repo)
        user_changes = self.uncommitted_changes(repo)

        if not self.has_commits(repo):
            if user_changes and not (mode == "new" or allow_git_init):
                raise GitSafetyError(
                    f"{repo} has no commits but contains uncommitted files. Commit a baseline yourself or set "
                    "\"allow_git_init\": true so the controller can back up and commit a baseline on the development branch."
                )
            if user_branch != dev_branch:
                self.run(["symbolic-ref", "HEAD", f"refs/heads/{dev_branch}"], repo)
                notes.append(f"Repository had no commits; unborn branch set to {dev_branch!r}.")
            if user_changes:
                archive = self.backup_directory(repo, backup_dir)
                notes.append(f"Backed up existing files to {archive}.")
                self.commit_changes(repo, "chore: baseline snapshot of existing files", None, protected)
            return WorkspaceInfo(repo, repo, dev_branch, None, self.head_commit(repo), "in-place", user_branch, [], notes)

        if mode == "new" and user_branch == dev_branch:
            # A repository the controller created for a new project: it works in place on the
            # development branch (there is no user branch to protect).
            return WorkspaceInfo(repo, repo, dev_branch, None, self.head_commit(repo), "in-place", user_branch, user_changes, notes)

        base_branch = default_branch if self.branch_exists(repo, default_branch) else user_branch
        base_ref = base_branch or "HEAD"

        if isolation == "worktree":
            for wt in self.list_worktrees(repo):
                wt_path = Path(wt.get("worktree", ""))
                if wt.get("branch") == f"refs/heads/{dev_branch}":
                    if wt_path.resolve() == repo.resolve():
                        raise GitSafetyError(
                            f"the development branch {dev_branch!r} is checked out in your main working tree; "
                            "switch your working tree to another branch or use git_isolation='branch'"
                        )
                    if wt_path.resolve() != worktree_path.resolve():
                        notes.append(f"Reusing existing worktree for {dev_branch!r} at {wt_path}.")
                        worktree_path = wt_path
                    break
            else:
                worktree_path.parent.mkdir(parents=True, exist_ok=True)
                if worktree_path.exists() and any(worktree_path.iterdir()):
                    raise GitSafetyError(f"worktree path {worktree_path} exists and is not empty; refusing to overwrite it")
                if self.branch_exists(repo, dev_branch):
                    self.run(["worktree", "add", str(worktree_path), dev_branch], repo)
                else:
                    self.run(["worktree", "add", "-b", dev_branch, str(worktree_path), base_ref], repo)
                notes.append(f"Created isolated worktree {worktree_path} on branch {dev_branch!r} from {base_ref}.")
            if user_changes:
                notes.append(
                    f"{len(user_changes)} uncommitted change(s) in your working tree were left untouched; "
                    "they are not part of the autonomous worktree."
                )
            workspace = worktree_path
        else:
            if user_changes:
                raise GitSafetyError(
                    "git_isolation='branch' requires a clean working tree, but uncommitted changes exist. "
                    "Commit or stash them yourself, or use the default worktree isolation."
                )
            if user_branch != dev_branch:
                if self.branch_exists(repo, dev_branch):
                    self.run(["checkout", dev_branch], repo)
                else:
                    self.run(["checkout", "-b", dev_branch], repo)
                notes.append(f"Switched working tree to development branch {dev_branch!r}.")
            workspace = repo

        branch_now = self.current_branch(workspace)
        if branch_now != dev_branch:
            raise GitSafetyError(f"workspace is on {branch_now!r}, expected {dev_branch!r}")
        return WorkspaceInfo(
            workspace_path=workspace,
            repo_path=repo,
            dev_branch=dev_branch,
            base_branch=base_branch,
            base_commit=self.head_commit(workspace),
            isolation=isolation,
            user_branch=user_branch,
            user_uncommitted_changes=user_changes,
            notes=notes,
        )
