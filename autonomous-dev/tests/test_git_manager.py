"""Git management safety tests: worktree isolation and user-change preservation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from git_manager import GitManager, GitSafetyError
from safe_exec import CommandPolicy, SafeExecutor


def _manager(tmp_path: Path, state) -> GitManager:
    policy = CommandPolicy(allowed_roots=[tmp_path], python_executables=[sys.executable])
    return GitManager(SafeExecutor(policy, state=state), "Autonomous Dev Agent", "agent@localhost")


def test_worktree_isolation_preserves_user_changes(tmp_path, state, user_repo):
    (user_repo / "app.py").write_text("print('user edit in progress')\n", encoding="utf-8")
    (user_repo / "notes.txt").write_text("untracked user notes\n", encoding="utf-8")
    gm = _manager(tmp_path, state)
    info = gm.prepare_workspace(
        user_repo, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", False, tmp_path / "backups"
    )
    assert info.workspace_path == tmp_path / "wt"
    assert gm.current_branch(info.workspace_path) == "autonomous-development"
    assert gm.current_branch(user_repo) == "main"
    # user's uncommitted work untouched and not copied into the worktree
    assert (user_repo / "app.py").read_text(encoding="utf-8") == "print('user edit in progress')\n"
    assert (user_repo / "notes.txt").exists()
    assert not (info.workspace_path / "notes.txt").exists()
    assert sorted(info.user_uncommitted_changes) == ["app.py", "notes.txt"]


def test_commit_never_goes_to_main(tmp_path, state, user_repo):
    gm = _manager(tmp_path, state)
    (user_repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(GitSafetyError):
        gm.commit_changes(user_repo, "feat: should fail", None, {"main"})


def test_commit_on_dev_branch_with_trailer_and_secret_exclusion(tmp_path, state, user_repo):
    gm = _manager(tmp_path, state)
    info = gm.prepare_workspace(user_repo, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", False, tmp_path / "b")
    ws = info.workspace_path
    (ws / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    sha = gm.commit_changes(ws, "feat: add feature", 7, {"main"})
    assert sha
    assert gm.find_task_commit(ws, 7) == sha
    remaining = gm.uncommitted_changes(ws)
    assert remaining == [".env"]  # secret file never staged


def test_reuse_existing_worktree_on_resume(tmp_path, state, user_repo):
    gm = _manager(tmp_path, state)
    first = gm.prepare_workspace(user_repo, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", False, tmp_path / "b")
    second = gm.prepare_workspace(user_repo, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", False, tmp_path / "b")
    assert first.workspace_path.resolve() == second.workspace_path.resolve()


def test_unversioned_project_requires_authorization(tmp_path, state):
    project = tmp_path / "plain"
    project.mkdir()
    (project / "main.py").write_text("print(1)\n", encoding="utf-8")
    gm = _manager(tmp_path, state)
    with pytest.raises(GitSafetyError):
        gm.prepare_workspace(project, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", False, tmp_path / "b")
    info = gm.prepare_workspace(project, "autonomous-development", "main", "worktree", tmp_path / "wt", "existing", True, tmp_path / "b")
    assert gm.current_branch(project) == "autonomous-development"
    assert any(p.suffix == ".zip" for p in (tmp_path / "b").iterdir())
    assert gm.has_commits(project)
    assert any("Backed up" in n for n in info.notes)


def test_branch_mode_refuses_dirty_tree(tmp_path, state, user_repo):
    (user_repo / "app.py").write_text("changed\n", encoding="utf-8")
    gm = _manager(tmp_path, state)
    with pytest.raises(GitSafetyError):
        gm.prepare_workspace(user_repo, "autonomous-development", "main", "branch", tmp_path / "wt", "existing", False, tmp_path / "b")
