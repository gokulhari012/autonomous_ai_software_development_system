# Autonomous development controller

A persistent, autonomous software-development system built on the **Claude Agent SDK for Python**.
It develops, reviews, tests, secures, fixes, deploys to local staging and validates **Flask** and
**PySide** applications, for **existing** or **new** projects, until every approved requirement is
independently validated, or until it needs a human.

The controller is independent of the application it works on: it lives in this directory and
operates on a project directory given in `config/project.json`.

---

## 1. Setup

Requirements: Python 3.11+ (3.13 tested), Git, Node.js 18+ (dashboard only), Windows or Linux.
Docker is optional.

```powershell
# Windows, from the repository root
powershell -ExecutionPolicy Bypass -File autonomous-dev\scripts\setup.ps1
```
```bash
# Linux / macOS
bash autonomous-dev/scripts/setup.sh
```

The setup script creates `.venv`, installs `requirements.txt`, installs Playwright Chromium,
builds the dashboard, creates the sample workspace and runs `python main.py doctor`.

### Claude authentication (required)

The SDK runs its bundled Claude Code CLI, which authenticates in one of two ways:

| Option | How | Notes |
|---|---|---|
| API key (recommended for unattended runs) | `ANTHROPIC_API_KEY=` in `autonomous-dev/.env` | Billed per token on your Anthropic Console account. |
| Claude subscription login | run `claude auth login` once (or be logged in to Claude Code) | Subject to your plan's usage limits. Costs shown are SDK **estimates**, not charges. |

A Claude subscription does not automatically give you API billing. `python main.py doctor` shows
which method is active (`Claude authentication` line).

### Telegram (optional, recommended)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and read `message.chat.id` (your chat id) and `message.from.id` (your user id).
3. Put them in `autonomous-dev/.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=<chat id>
   TELEGRAM_AUTHORIZED_USER_IDS=<your user id>
   ```
4. `python main.py telegram-test` sends a test message.

`.env` values are read into memory only. They are never exported to `os.environ`, so agents and
test runs cannot see them. `.env` is git-ignored; never commit it.

---

## 2. Running

All commands run from `autonomous-dev/` with the virtual environment's Python
(`..\.venv\Scripts\python.exe` on Windows, `../.venv/bin/python` on Linux).

```bash
python main.py doctor                 # environment, SDK, auth and tool checks
python main.py scan                   # what the controller detects in the project (no agents)
python main.py run                    # autonomous execution
python main.py run --with-telegram-bot --max-tasks 5
```

Point it at your own application by editing `config/project.json` (`project_path`, `project_name`),
or register further projects:

```bash
python main.py project add --name "My App" --path C:\code\my-app --mode existing
python main.py --config config/projects/my-app.json run
```

### Operating the controller

| Command | Effect |
|---|---|
| `status`, `tasks`, `agents`, `requirements`, `events --limit 100` | Inspect persisted state |
| `pause` / `resume` | Pause finishes the current safe operation (stage), then waits |
| `stop` | No new tasks or stages start; shuts down gracefully |
| `approvals`, `approve <id>`, `reject <id> --note ...` | Human decisions (technology stack, risky designs, security exceptions) |
| `approve-requirement REQ-002` | Approve an *inferred* requirement so its tasks may run |
| `retry-task <id>` / `cancel-task <id>` | Retry a BLOCKED task (retry counter reset) or cancel safely |
| `report` | Write the final project report to `reports/releases/` |
| `telegram-bot` | Run the Telegram command bot on its own (long polling) |

| `answer <id> --text "..."` | Answer a Product Manager question set (passed to the agents on the next run) |
| `project delete <id>` | Delete `config/projects/<id>.json` and hide the project; its folder and history are kept |
| `git-check` | Check the GitHub remote and that your git login works (read-only) |
| `resume-now` | Stop waiting for a usage-limit reset and retry immediately |
| `validate-config` | Check a project configuration file |

The same controls exist in Telegram (`/status /tasks /agents /logs /report /pause /resume /stop`)
and in the dashboard. All of them write persistent flags or commands, which the running
orchestrator applies at its next safe point. Telegram also has `/getip`, which replies with the
laptop's IP address. The bot answers only authorised users, while the controller runs with
`--with-telegram-bot` (or while `telegram-bot` runs).

### Opening the staging app from another device

Staging listens on `127.0.0.1` only. To open it from a phone or another computer on the same
network, enable **Let other devices on my network open the staging app** in the project settings
(`"flask": {"lan_access": true}`). The staging server then listens on all network interfaces, and
the Telegram staging message includes `Remote URL: http://<laptop-ip>:<staging port>`.

* Anyone on the same network can open it, so leave it off on public or shared Wi-Fi.
* Windows Firewall must allow incoming connections to Python on private networks.
* The staging app runs only while a task is being staged and validated, then it is stopped.
* It does not work from outside your network. Do not forward the port on your router.

### Everything from the dashboard

You can do all of the above in the dashboard without a terminal:

* **Projects**: create a new or existing project with a large description box (saved as
  `requirements/brief.md` for new projects; for existing projects it is given to the Product
  Manager as project notes), choose the application type, the finish point, GitHub push and
  auto-resume. You can also edit the description later, and delete a project (only its settings
  file; the folder is kept).
* **Control Panel**: Start (optionally limited to N tasks), Pause, Resume, Stop. Answer the
  Product Manager's questions and press **Save answers and resume**. Handle approvals and reports.
* **Task Manager**: Retry or Cancel tasks.
* **Settings**: every project option and the global limits (model, budget, run time, retries),
  validated by the controller before they are saved.
* **Troubleshooting**: what needs your attention and how to fix it, an environment check, a
  configuration check, a GitHub connection test, a Telegram test message, and live logs.

Installing Python packages is deliberately not possible from the browser; do that in a terminal.

### Finish point, GitHub push and auto-resume (project settings)

| Setting | Values |
|---|---|
| `stop_after` | `task` stops after each completed task; `requirements` (default) stops when no work is left; `manual` keeps running and waits for new work until you press Stop |
| `git_push_enabled`, `git_remote`, `git_remote_url` | Push the `autonomous-development` branch after every checkpoint commit. It never pushes `main`, never forces, and never deletes. Authentication uses the git login already on this computer (Git Credential Manager or an SSH key); no token is stored. A failed push is reported, and the next checkpoint retries it. |
| `auto_resume_on_limit` | When the Claude usage limit is reached, wait until it resets (plus one minute) and continue the interrupted stage. If the reset time is unknown it retries every 15 minutes. When off, the run stops cleanly with status `LIMIT_REACHED`, and you start it again later. Waiting time does not count against `max_execution_minutes`. |

Commit history for a new project: `chore: baseline snapshot` (your brief), then
`docs: add project brief, specification and architecture`, then one commit per completed task
(`chore: initialize project structure`, `feat: ...`, `fix: ...`, `security: ...`). Each commit body
lists what was implemented, what was fixed during review and testing, and the validated acceptance criteria.

### Offline demonstration (no Claude usage)

```bash
python scripts/demo_offline_pipeline.py
```

This runs the real orchestrator, with real Git, pytest, Playwright, Bandit, Semgrep, pip-audit and
Flask staging, but with scripted agents, into `database/demo_state.db`. To explore it in the
dashboard, start the dashboard with `DASHBOARD_DB_PATH` pointing at that file.

---

## 3. Architecture

```
autonomous-dev/
  main.py               CLI
  orchestrator.py       state machine, task loop, recovery, verification gates
  agent_runner.py       Claude Agent SDK integration (+ scripted test backend)
  agent_models.py       Pydantic models for every agent's structured output
  permissions.py        per-agent tool guard (PreToolUse hook)
  safe_exec.py          allowlisted command execution for the orchestrator
  state_manager.py      SQLite state, migrations, leases, locks
  task_manager.py       requirements registry, tasks, approvals, control commands
  project_inspector.py  technology detection (Flask / PySide / DB / tests / mode)
  test_manager.py       pytest, Playwright, pytest-qt, PyAutoGUI layers, JUnit parsing
  security_scanner.py   Bandit, Semgrep, pip-audit + severity policy
  deployment_manager.py local staging (process / docker compose / desktop launch)
  git_manager.py        safe git (worktree isolation, checkpoints)
  telegram_notifier.py  milestone notifications (queue, retry, dedupe)
  telegram_bot.py       authorised command handling (long polling)
  report_generator.py   final and per-task reports
  events.py             structured events (dashboard + audit trail)
  agents/*.md           nine agent definitions (YAML front matter = enforced policy)
  config/               project.json, workflow.json, testing.json, security.json, semgrep rules
  pytest_plugins/       evidence capture injected into target test runs
  sample_apps/          Flask and PySide sample targets (Phase 8)
  tests/                the controller's own test-suite
```

### Workflow (state machine)

```
PROJECT_SCAN -> REQUIREMENT_ANALYSIS (existing) | DISCOVERY -> SPECIFICATION -> ARCHITECTURE (new)
             -> TASK_PLANNING
per task:  ARCHITECTURE_DESIGN -> DEVELOPMENT -> CODE_REVIEW -> QA_TESTING -> SECURITY_TESTING
           -> LOCAL_STAGING_DEPLOYMENT -> PRODUCT_VALIDATION -> GIT_CHECKPOINT -> COMPLETED
failures:  review / QA / security / staging / validation -> BUG_FIXING -> CODE_REVIEW -> QA -> SECURITY ...
```

* Transitions are checked against an explicit table, so mandatory stages cannot be skipped.
* Every failure that needs a repair counts against `max_retries_per_task` (default 3). The next
  failure after that marks the task **BLOCKED**, sends a Telegram notification, and lets the loop
  continue with other independent tasks.
* Agent or infrastructure errors retry the same stage up to `max_stage_errors` times, with
  exponential backoff for transient API errors inside the agent runner.

### Independent verification (agents are never trusted)

| Gate | PASS only if |
|---|---|
| Development | `git` shows real changes and the changed Python files compile |
| Code review | reviewer PASS **and** no HIGH/CRITICAL finding **and** every acceptance criterion met |
| QA | the orchestrator's own pytest / Playwright / pytest-qt runs pass (exit codes + JUnit XML). Missing tools are BLOCKED, never PASS |
| Security | scanners ran (report parsed), and the severity policy holds; HIGH/CRITICAL can only be waived by a human-approved exception |
| Staging | health endpoint returns 200 (and reports a working DB), tests pass against staging |
| Validation | validator recommends completion **and** every criterion has evidence tests **and** the orchestrator re-runs those tests itself **and** the regression suite passes |
| Requirement complete | all its tasks completed with stored validation evidence (enforced in the database layer too) |

### Agents and permissions

| Agent | Tools | May write |
|---|---|---|
| product_manager | Read, Glob, Grep, Write, Edit | `docs/**` |
| architect | Read, Glob, Grep, Write, Edit | `docs/**` |
| developer | + Bash (pytest, compileall) | task scope, tests, docs, README, requirements*.txt |
| code_reviewer | Read, Glob, Grep | nothing |
| qa_engineer | + Bash (pytest) | `tests/**` |
| security_auditor | Read, Glob, Grep | nothing |
| bug_fixer | + Bash (pytest, compileall) | task scope, files named in issues, tests |
| devops_engineer | Read, Glob, Grep, Write, Edit | staging config only (Dockerfile, staging compose, `deploy/**`) |
| product_validator | + Bash (pytest) | `tests/acceptance/**` |

Enforcement does not rely on Claude's permission settings alone:

* `ClaudeAgentOptions(tools=..., allowed_tools=..., permission_mode="dontAsk", setting_sources=[])`
  ensures no prompts, and no project-supplied settings or hooks are loaded.
* A **PreToolUse hook** (`permissions.ToolGuard`) checks every call. Reads must stay inside the
  workspace and away from secret files. Writes must match the write scope above; `.git` and `.env`
  are always protected. Bash allows only allowlisted prefixes, with no `; & | > $( )` and no
  background jobs. Web, MCP, sub-agent and git tools are denied.
* Inherited secret-looking environment variables are blanked for the CLI. The orchestrator's own
  commands (`safe_exec`) run without a shell, from a structural allowlist, with a minimal
  environment and an audit log.
* On Linux/macOS, Claude Code's OS-level Bash sandbox is enabled as an extra layer (it is not
  available on Windows).
* Recommended: run the controller under a **dedicated OS user** that can only access the project
  directory. That OS-level boundary is outside what the controller can set up for you.

### Git rules

The controller works in a **git worktree** on `autonomous-development` (`workspaces/<project>/`).
Your own working tree and uncommitted changes are never touched. It never pushes, force-pushes,
resets, cleans, rebases, amends, deletes branches or commits to `main`. A repository without Git
is only initialised when `allow_git_init: true` is set: the directory is zipped to `backups/`
first, then a baseline is committed on the development branch. Each validated task becomes one
commit (`feat: ...`, `fix: ...`, `security: ...`) with an `Autonomous-Task: <id>` trailer; the
trailer also makes crash recovery idempotent.

### Persistence and recovery

All state is in `database/agent_state.db` (SQLite, WAL, versioned migrations, transactional
transitions). On start the controller:

* takes the project lock, refusing to start if another live controller holds it;
* marks interrupted stage and agent runs;
* stops staging processes left running (verified by pid **and** process start time);
* releases expired task leases;
* checks the Git branch, any in-progress merge or rebase, and uncommitted work.

It then resumes every task from its last persisted stage; an interrupted stage is re-run with a
note to the agent to inspect the current state first.

### Testing layers (chosen from what is detected)

| Layer | Used when | Notes |
|---|---|---|
| pytest (unit/API/DB/integration) | any Python project | isolated test DB via `DATABASE_URL` |
| Playwright (`-m e2e`) | Flask app **with** a browser UI | Chromium; screenshots, traces, console and network logs on failure |
| pytest-qt (`-m qt`) | PySide2/6 app | `QT_QPA_PLATFORM=offscreen`; window screenshots on failure |
| PyAutoGUI (`-m gui`) | PySide app + real desktop + `allow_real_input_automation: true` | otherwise **BLOCKED**, never PASS |
| acceptance | validation stage | written by the Product Validator, re-run by the orchestrator |

Reports are written under `reports/tests/<project>/task-N/...`: JUnit XML, JSON, logs and artifacts.

### Security policy (`config/security.json`)

* Bandit, Semgrep (registry rulesets `p/python p/flask p/security-audit p/secrets` plus
  `config/semgrep/local-rules.yml`) and pip-audit. If the registry cannot be reached, Semgrep
  re-runs with the local rules and the reduced coverage is reported.
* `block_severity_threshold: HIGH`. Scanner errors block by default (`scanner_failure_policy`).
* The auditor may dismiss only LOW and MEDIUM findings. HIGH and CRITICAL findings are waived only
  by a human, through an entry in `config/security_exceptions.json` with `fingerprint`,
  `approved_by` and `reason`.
* Pre-existing CRITICAL findings in untouched code create a remediation task and block release.

### Limits (`config/workflow.json`)

`max_tasks_per_run`, `max_agent_turns_per_task`, per-agent `max_turns`, `agent_timeout_seconds`,
`max_execution_minutes`, `max_budget_usd` (per run, measured from SDK-reported cost) and
`max_budget_usd_per_agent_call`. The model is `claude-opus-5-5`, with fallback `claude-sonnet-5-5`;
per-agent `effort` is set in the same file. `max_parallel_tasks` (default 1) runs independent tasks
concurrently with file reservations. Test runs, scans and commits are serialised.

### Agent team, automatic models and upgrades

`agent_team` in a project file chooses which agents work on its tasks (`agent_selection.py`):

* `"mode": "full"` (default): every agent runs.
* `"mode": "custom"`: the core agents plus those listed in `"agents"`.
* `"mode": "auto"`: each task gets the agents it needs, judged from its kind, size and scope.
  Simple, documentation and test tasks skip the Architect and QA Engineer. They also skip the
  Security Auditor unless they touch security-sensitive code. The plan is fixed when the task
  starts and is reported as an `AGENT_TEAM_SELECTED` event.

The Product Manager, Developer, Code Reviewer and Product Validator always run. Here is what
happens when an optional agent is not on the team:

| Agent | Without it |
|---|---|
| Architect | The Developer works from the task description; there is no design step. |
| QA Engineer | The orchestrator still runs the full test suite, and failures go straight to repair. |
| Security Auditor | Bandit, Semgrep and pip-audit still run, and the blocking policy still applies. |
| Bug Fixer | The Developer fixes the open review, test, scan and staging issues. |
| DevOps Engineer | A staging app that will not start is recorded as an issue and sent for repair. |

In `workflow.json`:

* `auto_model_selection: true` picks the model and effort from the task's complexity, and the
  per-agent `model`/`effort` settings are then ignored:

  | Task complexity | Model | Effort |
  |---|---|---|
  | Simple | Sonnet 5.5 | medium |
  | Standard | Opus 5.5 | high |
  | Complex | Opus 5.5 | xhigh |

  The QA Engineer and DevOps Engineer run one effort level lower.
* `auto_escalation: true` retries an agent one step stronger after each failed attempt. A failed
  attempt is an error, an invalid output, or, for the Developer and Bug Fixer, work sent back for
  repair. Effort rises first, then the model, up to `escalation_max_model` (Sonnet 5.5, Opus 5.5
  or Fable 5.2); the last step is effort `max`. It never downgrades. The chosen model and effort
  appear in each `AGENT_STARTED` event.

### Token saving with RTK (Rust Token Killer)

[RTK](https://github.com/rtk-ai/rtk) is a separate program that shortens command output. For
example, `rtk pytest` shows only the failing tests. It's off by default. To turn it on:

1. Install it: on Windows, run `winget install rtk-ai.rtk`. Alternatively, download
   `rtk-x86_64-pc-windows-msvc.zip` from the releases page and put `rtk.exe` on PATH. Don't use
   `cargo install rtk`, which installs an unrelated "Rust Type Kit".
2. In the dashboard, open **Settings → Token saving**, tick **Use RTK**, and save. If `rtk` isn't
   on PATH, also set the full path to `rtk.exe`. In `workflow.json`, the keys are
   `rtk_enabled` and `rtk_path`.
3. Press **Check RTK**, or run `python main.py rtk-check`. Doctor also shows the status.

How it works:

* Agents run with `setting_sources=[]`, so a global `rtk init -g` hook for your own Claude Code
  sessions doesn't reach them. The controller's `PreToolUse` hook uses RTK instead.
* The hook first checks the original command against the agent's allowlist, and only then asks
  `rtk rewrite` for a shorter form.
* A rewrite is used only when it's `rtk pytest`, with arguments taken from the original command,
  so RTK can't widen what an agent may run. `python -m compileall` and every other command run
  unchanged.
* When RTK cuts output short, agents may run `rtk recall <id>` to see the full output. The full
  output is kept per project in `database/rtk-recall-<project>.db`. RTK telemetry is disabled
  for these runs.
* If RTK is missing, the wrong program, too old (below 0.25), or slow (over 3 s), agents run the
  normal command. An `RTK_STATUS` event shows which case applies.
* The controller's own authoritative test runs and reports never use RTK.

---

## 4. Running the controller's own tests

```bash
python -m pytest                       # 92 tests; real tools, scripted agents (about 6 minutes)
python -m pytest -m "not slow"         # quick subset
python -m pytest --run-live -m live    # calls the real Claude Agent SDK (uses plan usage / API credit)
```

---

## 5. Verification status (on the build machine: Windows 11, Python 3.13)

Verified:

* The whole state machine against real tools, with scripted agents, on both samples. That covers
  worktree isolation, commits on the dev branch with `main` untouched, real pytest, Playwright
  (Chromium), pytest-qt, Bandit, Semgrep, pip-audit, Flask staging health checks and an offscreen
  PySide launch.
* The Flask pipeline in detail: the scanners catch an injected SQL injection, the fix is
  re-reviewed, re-tested and re-scanned, and a false PASS claim from the validator is rejected by
  the orchestrator's own re-run.
* Stop and resume from the persisted stage; crash recovery (dead lock holder, interrupted stage and
  agent run); refusal of a second live controller.
* The retry limit leading to BLOCKED while other tasks continue; approval gating; the new-project
  flow (brief, questions, technology-stack approval, planning).
* Telegram notification and command handling against a mocked Bot API.
* Live Claude Agent SDK calls: structured output, the PreToolUse hook and `dontAsk` mode work with
  the bundled CLI 2.1.332.

Not verified here:

* **Docker Compose staging**: implemented, but Docker is not installed on this machine, so Flask
  staging used the local-process method.
* **Real Telegram delivery**: needs your bot token; run `python main.py telegram-test`.
* **PyAutoGUI tests**: they move the real mouse and keyboard, so they are disabled by default
  (reported BLOCKED). Enable them only on a dedicated test desktop.
* **Packaged-executable testing**: implemented (`pyside.package_command` / `executable_path`), but
  the samples do not build an executable.
* **A full multi-hour autonomous run with real Claude agents**: not executed during the build.
  Start one with `python main.py run`.

* **New project**:
`cd autonomous-dev`
1. Create New Project
`..\.venv\Scripts\python.exe main.py project add --name "My App" --path C:\code\my-app --mode new`
2. Run the New project
`..\.venv\Scripts\python.exe main.py --config config\projects\my-app.json run`
It will create "my-app/requirements/brief.md" 
need to answer that questions.
3. Again run
`..\.venv\Scripts\python.exe main.py --config config\projects\my-app.json run`
If any doubts it will create It will create "my-app/requirements/open_questions.md" 
need to answer that questions.
4. Again run
`..\.venv\Scripts\python.exe main.py --config config\projects\my-app.json run`