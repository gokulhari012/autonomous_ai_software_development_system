"""Command-line entry point for the autonomous development controller.

Examples (run from the autonomous-dev directory)::

    python main.py doctor                 # verify environment, SDK, auth, tools
    python main.py scan                   # detect the project's technology (no agents)
    python main.py run                    # autonomous execution until done/blocked/limits
    python main.py run --with-telegram-bot
    python main.py status | tasks | agents | events
    python main.py pause | resume | stop
    python main.py retry-task 4 | cancel-task 5
    python main.py approvals | approve 3 | reject 3 --note "use PostgreSQL"
    python main.py approve-requirement REQ-002
    python main.py report
    python main.py telegram-bot | telegram-test
    python main.py project add --name "My App" --path ../my-app --mode existing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import CONFIG_DIR, CONTROLLER_DIR, LOGS_DIR, ConfigError, ensure_runtime_dirs, load_config, slugify
from logging_setup import register_secret_values, setup_logging
from state_manager import StateManager
from status_reports import agents_summary, control_state, overall_status, tasks_summary


def _load(args):
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    ensure_runtime_dirs()
    setup_logging(LOGS_DIR, cfg.secrets.log_level, console=not getattr(args, "quiet", False))
    register_secret_values(cfg.secrets.secret_values())
    return cfg


def _orchestrator_running(state: StateManager, project_id: str) -> bool:
    lock = state.get_lock(f"orchestrator:{project_id}")
    return bool(lock and lock["expires_at"] > time.time())


def _submit(cfg, command: str, args: dict, actor: str = "cli") -> None:
    from task_manager import TaskManager

    state = StateManager(cfg.db_path)
    if _orchestrator_running(state, cfg.project_id):
        command_id = state.add_control_command(cfg.project_id, command, args, actor)
        print(f"Queued {command} (#{command_id}); the running orchestrator applies it at the next safe point.")
        return
    ok, message = TaskManager(state, None, cfg.project_id, cfg.project.max_retries_per_task, cfg.project.auto_approve_inferred_requirements).apply_command(command, args, actor)
    print(("OK: " if ok else "Rejected: ") + message)


# ------------------------------------------------------------------ doctor
def cmd_doctor(args) -> int:
    cfg = _load(args)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python >= 3.11", sys.version_info >= (3, 11), platform.python_version()))
    git = shutil.which("git")
    checks.append(("Git", bool(git), subprocess.run([git, "--version"], capture_output=True, text=True).stdout.strip() if git else "not found"))
    try:
        import claude_agent_sdk

        sdk_ok, sdk_info = True, f"claude-agent-sdk {claude_agent_sdk.__version__}"
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
        cli = str(bundled) if bundled.exists() else shutil.which("claude")
    except ImportError:
        sdk_ok, sdk_info, cli = False, "not installed (pip install claude-agent-sdk)", None
    checks.append(("Claude Agent SDK", sdk_ok, sdk_info))
    auth_detail, auth_ok = "unknown", False
    if cfg.secrets.anthropic_api_key:
        auth_ok, auth_detail = True, "ANTHROPIC_API_KEY configured in .env (API billing applies)"
    elif cli:
        try:
            out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=60).stdout
            data = json.loads(out)
            auth_ok = bool(data.get("loggedIn"))
            auth_detail = f"logged in via {data.get('authMethod')} ({data.get('subscriptionType') or data.get('apiProvider')}); plan usage limits apply" if auth_ok else "not logged in - run 'claude auth login' or set ANTHROPIC_API_KEY"
        except Exception as exc:
            auth_detail = f"could not query CLI auth status: {exc}"
    checks.append(("Claude authentication", auth_ok, auth_detail))
    py = cfg.project.python_executable or sys.executable

    def has(module: str) -> bool:
        return subprocess.run([py, "-m", "pip", "show", module], capture_output=True, text=True).returncode == 0

    from test_manager import playwright_browser_installed
    from project_inspector import gui_session_available

    for label, module in (("pytest", "pytest"), ("pytest-playwright", "pytest-playwright"), ("pytest-qt", "pytest-qt"), ("PyAutoGUI", "PyAutoGUI"), ("Bandit", "bandit"), ("pip-audit", "pip-audit"), ("pytest-timeout", "pytest-timeout")):
        checks.append((label, has(module), "installed" if has(module) else f"pip install {module}"))
    checks.append(("Playwright Chromium", playwright_browser_installed(), "installed" if playwright_browser_installed() else "python -m playwright install chromium"))
    from safe_exec import find_executable

    semgrep = find_executable("semgrep")
    checks.append(("Semgrep", bool(semgrep), semgrep or "pip install semgrep"))
    docker = shutil.which("docker")
    checks.append(("Docker (optional)", True, "available" if docker else "not installed - Flask staging uses a local process instead"))
    gui_ok, gui_reason = gui_session_available()
    checks.append(("GUI session (optional)", True, gui_reason + ("" if cfg.testing.pyside.allow_real_input_automation else "; real input automation disabled by config")))
    tg = cfg.secrets
    checks.append(("Telegram (optional)", True, ("configured" if tg.telegram_enabled and tg.telegram_chat_id else "not configured - notifications are recorded only") + (f", {len(tg.telegram_authorized_user_ids)} authorised user(s)" if tg.telegram_authorized_user_ids else "")))
    checks.append(("Project path", cfg.project_dir.exists() or cfg.project.project_mode == "new", str(cfg.project_dir)))
    width = max(len(c[0]) for c in checks)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    print("\nAll required checks passed." if not failed else f"\n{failed} required check(s) failed.")
    return 1 if failed else 0


# ------------------------------------------------------------------- scan
def cmd_scan(args) -> int:
    cfg = _load(args)
    from project_inspector import inspect_project

    profile = inspect_project(cfg.project_dir, cfg.project.project_brief)
    print(profile.model_dump_json(indent=2))
    return 0


# -------------------------------------------------------------------- run
def cmd_run(args) -> int:
    cfg = _load(args)
    from orchestrator import Orchestrator, StopRun

    if args.max_tasks:
        cfg.project.max_tasks_per_run = args.max_tasks
    try:
        orchestrator = Orchestrator(cfg)
        summary = asyncio.run(orchestrator.run(with_telegram_bot=args.with_telegram_bot))
    except StopRun as exc:
        print(f"Not started: {exc.reason}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Interrupted. State is saved; run again to resume.", file=sys.stderr)
        return 130
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] in ("COMPLETED", "STOPPED", "AWAITING_INPUT") else 1


# ---------------------------------------------------------- status views
def cmd_status(args) -> int:
    cfg = _load(args)
    state = StateManager(cfg.db_path)
    print(overall_status(state, cfg.project_id))
    pending = state.list_approvals(cfg.project_id, "PENDING")
    if pending:
        print(f"\n{len(pending)} approval(s) pending - see: python main.py approvals")
    return 0


def cmd_tasks(args) -> int:
    cfg = _load(args)
    print(tasks_summary(StateManager(cfg.db_path), cfg.project_id, limit=200))
    return 0


def cmd_agents(args) -> int:
    cfg = _load(args)
    print(agents_summary(StateManager(cfg.db_path), cfg.project_id))
    return 0


def cmd_events(args) -> int:
    cfg = _load(args)
    state = StateManager(cfg.db_path)
    rows = state.query("SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (cfg.project_id, args.limit))
    for e in reversed(rows):
        print(f"{e['ts']} {e['event_type']:<22} {('#' + str(e['task_id'])) if e['task_id'] else '':<5} {e['agent_name'] or '':<18} {e['summary'][:150]}")
    return 0


def cmd_requirements(args) -> int:
    cfg = _load(args)
    for r in StateManager(cfg.db_path).list_requirements(cfg.project_id):
        print(f"{r['code']} [{r['status']}] (P{r['priority']}, {r['source']}) {r['title']}")
    return 0


# --------------------------------------------------------------- controls
def cmd_flag(args) -> int:
    cfg = _load(args)
    state = StateManager(cfg.db_path)
    pid = cfg.project_id
    if args.command == "pause":
        state.set_flag(f"pause:{pid}", "1", "cli")
        print("Pause requested.")
    elif args.command == "resume":
        state.set_flag(f"pause:{pid}", "0", "cli")
        state.set_flag(f"stop:{pid}", "0", "cli")
        print("Resumed." + ("" if _orchestrator_running(state, pid) else " The controller is not running; start it with: python main.py run"))
    elif args.command == "stop":
        state.set_flag(f"stop:{pid}", "1", "cli")
        print("Stop requested; the controller finishes the current safe operation and shuts down.")
    state.audit("cli", args.command, pid)
    print(f"Controller state: {control_state(state, pid)}")
    return 0


def cmd_task_command(args) -> int:
    cfg = _load(args)
    command = {"retry-task": "RETRY_TASK", "cancel-task": "CANCEL_TASK"}[args.command]
    _submit(cfg, command, {"task_id": args.task_id})
    return 0


def cmd_requirement_decision(args) -> int:
    cfg = _load(args)
    command = "APPROVE_REQUIREMENT" if args.command == "approve-requirement" else "REJECT_REQUIREMENT"
    _submit(cfg, command, {"code": args.code})
    return 0


def cmd_approvals(args) -> int:
    cfg = _load(args)
    rows = StateManager(cfg.db_path).list_approvals(cfg.project_id)
    if not rows:
        print("No approvals.")
    for row in rows:
        print(f"#{row['id']} [{row['status']}] {row['kind']} {row['subject']}: {json.dumps(row.get('details'))[:300]}")
    return 0


def cmd_decide(args) -> int:
    cfg = _load(args)
    _submit(cfg, "APPROVE" if args.command == "approve" else "REJECT", {"approval_id": args.approval_id, "note": args.note or ""})
    return 0


def cmd_report(args) -> int:
    cfg = _load(args)
    from report_generator import generate_final_report

    md, js, criteria = generate_final_report(cfg, StateManager(cfg.db_path))
    print(f"Report written:\n  {md}\n  {js}\nProject complete: {criteria['project_complete']}")
    return 0


# --------------------------------------------------------------- telegram
def cmd_telegram_bot(args) -> int:
    cfg = _load(args)
    from telegram_bot import TelegramCommandBot

    bot = TelegramCommandBot(cfg, StateManager(cfg.db_path))
    if not bot.enabled:
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).", file=sys.stderr)
        return 2
    stop = asyncio.Event()
    print("Telegram command bot running (long polling). Ctrl+C to stop.")
    try:
        asyncio.run(bot.run(stop))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_telegram_test(args) -> int:
    cfg = _load(args)
    from telegram_notifier import TelegramNotifier

    state = StateManager(cfg.db_path)
    notifier = TelegramNotifier(cfg.secrets, state, cfg.project_id, cfg.project.project_name, 0)
    if not notifier.enabled:
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).", file=sys.stderr)
        return 2
    notifier.notify("SYSTEM_RESUMED", "Telegram test message", {"Status": "OK"}, ["If you can read this, notifications work."], dedupe_key=f"test-{time.time()}")
    sent = asyncio.run(notifier.flush())
    last = state.list_notifications(1)[0]
    print(f"sent={sent} status={last['status']} error={last.get('last_error')}")
    return 0 if sent else 1


# ---------------------------------------------------------------- project
def cmd_project(args) -> int:
    if args.project_command == "list":
        cfg = _load(args)
        for row in StateManager(cfg.db_path).list_projects():
            print(f"{row['id']}: {row['name']} [{row['status']}] mode={row['mode']} path={row['path']} config={row['config_path']}")
        for path in sorted((CONFIG_DIR / "projects").glob("*.json")):
            print(f"config file: {path}")
        return 0
    project_path = Path(args.path).expanduser().resolve()
    if args.mode == "existing" and not project_path.exists():
        print(f"{project_path} does not exist", file=sys.stderr)
        return 2
    base = json.loads((CONFIG_DIR / "project.json").read_text(encoding="utf-8"))
    slug = slugify(args.name)
    base.update({"project_name": args.name, "project_path": str(project_path), "project_mode": args.mode, "project_id": slug})
    target = CONFIG_DIR / "projects" / f"{slug}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    target.write_text(json.dumps(base, indent=2), encoding="utf-8")
    args.config = str(target)
    cfg = _load(args)
    StateManager(cfg.db_path).upsert_project(slug, name=args.name, path=str(project_path), mode=None if args.mode == "auto" else args.mode, config_path=str(target), status="REGISTERED")
    print(f"Project registered: {slug}\nConfig: {target}\nRun with: python main.py --config {target} run")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous AI software development controller")
    parser.add_argument("--config", help="project configuration JSON (default: config/project.json)")
    parser.add_argument("--quiet", action="store_true", help="no console logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check environment, SDK, authentication and tools").set_defaults(func=cmd_doctor)
    sub.add_parser("scan", help="detect the project technology stack").set_defaults(func=cmd_scan)
    run = sub.add_parser("run", help="run the autonomous workflow")
    run.add_argument("--max-tasks", type=int)
    run.add_argument("--with-telegram-bot", action="store_true", help="also answer Telegram commands while running")
    run.set_defaults(func=cmd_run)
    for name, func in (("status", cmd_status), ("tasks", cmd_tasks), ("agents", cmd_agents), ("requirements", cmd_requirements), ("approvals", cmd_approvals), ("report", cmd_report),
                       ("telegram-bot", cmd_telegram_bot), ("telegram-test", cmd_telegram_test)):
        sub.add_parser(name).set_defaults(func=func)
    events = sub.add_parser("events")
    events.add_argument("--limit", type=int, default=50)
    events.set_defaults(func=cmd_events)
    for name in ("pause", "resume", "stop"):
        sub.add_parser(name).set_defaults(func=cmd_flag)
    for name in ("retry-task", "cancel-task"):
        p = sub.add_parser(name)
        p.add_argument("task_id", type=int)
        p.set_defaults(func=cmd_task_command)
    for name in ("approve-requirement", "reject-requirement"):
        p = sub.add_parser(name)
        p.add_argument("code")
        p.set_defaults(func=cmd_requirement_decision)
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("approval_id", type=int)
        p.add_argument("--note")
        p.set_defaults(func=cmd_decide)
    project = sub.add_parser("project")
    psub = project.add_subparsers(dest="project_command", required=True)
    add = psub.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--path", required=True)
    add.add_argument("--mode", choices=["auto", "new", "existing"], default="auto")
    add.add_argument("--force", action="store_true")
    psub.add_parser("list")
    project.set_defaults(func=cmd_project)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.chdir(CONTROLLER_DIR)
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
