"""Telegram command bot (long polling ``getUpdates``).

Supported commands: /status /tasks /agents /logs /report /pause /resume /stop /help

Security:
* a command is accepted only when it comes from the configured chat
  (``TELEGRAM_CHAT_ID``) **and** from a user listed in
  ``TELEGRAM_AUTHORIZED_USER_IDS``; everything else is ignored silently
  (no reply, so the bot does not reveal itself) and written to the audit log;
* replies never include secrets: logs are redacted and truncated;
* /pause, /resume and /stop only set persistent control flags. The orchestrator
  honours them at the next safe point (pause finishes the current operation,
  stop prevents new tasks and shuts down gracefully).
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Callable

import httpx

from config import LOGS_DIR, AppConfig
from logging_setup import redact, tail_log
from state_manager import StateManager
from status_reports import agents_summary, latest_report, overall_status, tasks_summary

log = logging.getLogger("telegram_bot")

HELP = (
    "/status - overall project status\n/tasks - pending and completed tasks\n/agents - agent activity\n"
    "/logs - recent logs\n/report - latest project report\n/pause - pause new task scheduling\n"
    "/resume - resume execution\n/stop - safely stop new task execution"
)


class TelegramCommandBot:
    def __init__(self, config: AppConfig, state: StateManager, client_factory: Callable[[], httpx.AsyncClient] | None = None, api_base: str = "https://api.telegram.org"):
        self.config = config
        self.state = state
        self.project_id = config.project_id
        self.secrets = config.secrets
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(40.0)))
        self.api_base = api_base.rstrip("/")
        self.offset_key = f"telegram_offset:{self.project_id}"

    @property
    def enabled(self) -> bool:
        return bool(self.secrets.telegram_bot_token and self.secrets.telegram_chat_id)

    def is_authorized(self, message: dict[str, Any]) -> bool:
        chat_id = str(message.get("chat", {}).get("id", ""))
        user_id = str(message.get("from", {}).get("id", ""))
        if not self.secrets.telegram_authorized_user_ids:
            return False
        return chat_id == str(self.secrets.telegram_chat_id) and user_id in self.secrets.telegram_authorized_user_ids

    def handle_command(self, text: str, user_id: str) -> str:
        command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
        pid = self.project_id
        if command in ("/start", "/help"):
            return HELP
        if command == "/status":
            return overall_status(self.state, pid)
        if command == "/tasks":
            return tasks_summary(self.state, pid)
        if command == "/agents":
            return agents_summary(self.state, pid)
        if command == "/logs":
            lines = [l for l in tail_log(LOGS_DIR, 60) if " DEBUG " not in l][-25:]
            return "\n".join(l[:220] for l in lines) or "No log lines yet."
        if command == "/report":
            _, summary = latest_report(pid)
            return summary
        actor = f"telegram:{user_id}"
        if command == "/pause":
            self.state.set_flag(f"pause:{pid}", "1", actor)
            self.state.audit(actor, "pause", pid)
            return "Pause requested. The current safe operation will finish, then no new work starts until /resume."
        if command == "/resume":
            self.state.set_flag(f"pause:{pid}", "0", actor)
            self.state.set_flag(f"stop:{pid}", "0", actor)
            self.state.audit(actor, "resume", pid)
            return "Resume requested. If the controller is not running, start it with: python main.py run"
        if command == "/stop":
            self.state.set_flag(f"stop:{pid}", "1", actor)
            self.state.audit(actor, "stop", pid)
            return "Stop requested. No new tasks will start; the controller shuts down after the current safe operation."
        return "Unknown command.\n" + HELP

    async def _reply(self, client: httpx.AsyncClient, chat_id: Any, text: str) -> None:
        body = f"<pre>{html.escape(redact(text))[:3900]}</pre>"
        try:
            await client.post(f"{self.api_base}/bot{self.secrets.telegram_bot_token}/sendMessage", json={"chat_id": chat_id, "text": body, "parse_mode": "HTML"})
        except httpx.HTTPError as exc:
            log.warning("telegram reply failed: %s", type(exc).__name__)

    async def process_update(self, client: httpx.AsyncClient, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message or not str(message.get("text", "")).startswith("/"):
            return
        user_id = str(message.get("from", {}).get("id", ""))
        if not self.is_authorized(message):
            self.state.audit(f"telegram:{user_id}", "unauthorized_command", str(message.get("text", ""))[:100], {"chat": message.get("chat", {}).get("id")}, allowed=False)
            log.warning("ignored Telegram command from unauthorised user %s", user_id)
            return
        reply = self.handle_command(str(message["text"]), user_id)
        await self._reply(client, message["chat"]["id"], reply)

    async def poll_once(self, client: httpx.AsyncClient, timeout: int = 25) -> int:
        offset = int(self.state.kv_get(self.offset_key, "0") or 0)
        url = f"{self.api_base}/bot{self.secrets.telegram_bot_token}/getUpdates"
        response = await client.get(url, params={"timeout": timeout, "offset": offset, "allowed_updates": '["message","edited_message"]'})
        if response.status_code == 409:
            raise RuntimeError("another getUpdates consumer or a webhook is active for this bot")
        response.raise_for_status()
        updates = response.json().get("result", [])
        for update in updates:
            try:
                await self.process_update(client, update)
            finally:
                self.state.kv_set(self.offset_key, str(int(update["update_id"]) + 1))
        return len(updates)

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            log.info("Telegram command bot disabled (no token/chat id configured)")
            return
        if not self.secrets.telegram_authorized_user_ids:
            log.warning("TELEGRAM_AUTHORIZED_USER_IDS is empty: all Telegram commands will be ignored")
        backoff = 2.0
        async with self.client_factory() as client:
            while not stop.is_set():
                try:
                    await self.poll_once(client)
                    backoff = 2.0
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    log.warning("telegram polling error: %s (retrying in %.0fs)", redact(str(exc))[:200], backoff)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 120)
