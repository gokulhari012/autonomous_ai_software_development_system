# Autonomous AI Software Development System

An autonomous development, testing and local-staging system for Flask and PySide applications,
built on the Claude Agent SDK for Python. Nine specialised agents work under a central,
state-machine-based orchestrator, which verifies every result independently with real tools.

| Folder | What it is |
|---|---|
| [autonomous-dev/](autonomous-dev/) | The controller (orchestrator, agents, testing, security, deployment, Telegram). Start with its [README](autonomous-dev/README.md). |
| [dashboard/](dashboard/) | Separate web dashboard (FastAPI + React) for monitoring and control. See its [README](dashboard/README.md). |
| `sample-workspace/` | Created by the setup script: sample "existing projects" with their own Git repositories (git-ignored). |

Quick start (Windows):

```powershell
powershell -ExecutionPolicy Bypass -File autonomous-dev\scripts\setup.ps1
cd autonomous-dev
..\.venv\Scripts\python.exe main.py doctor
..\.venv\Scripts\python.exe main.py run
```

The original specification is in [autonomous-dev/prompts/master_prompt.md](autonomous-dev/prompts/master_prompt.md).
