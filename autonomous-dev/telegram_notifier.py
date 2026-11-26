"""Telegram milestone notifications via the official Bot API (``sendMessage``).

* Messages are queued in the SQLite ``notifications`` table first, so nothing is
  lost if Telegram (or the network) is down; a worker flushes the queue.
* Duplicate milestone messages inside the dedupe window are dropped.
* All dynamic text is HTML-escaped (``parse_mode=HTML``) and passed through the
  secret redactor; long messages are truncated to Telegram's 4096 character limit.
* Transient failures (network, HTTP 429 with ``retry_after``, 5xx) are retried
  with backoff; permanent failures are recorded and logged. A notification
  failure never interrupts the development workflow.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Callable

import httpx

from config import Secrets
from logging_setup import redact
from state_manager import StateManager, fingerprint

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
MAX_LEN = 4000
MAX_ATTEMPTS = 5

EMOJI = {
    "TASK_STARTED": "🚀", "TASK_COMPLETED": "✅", "DEVELOPMENT_PROGRESS": "🛠️", "CODE_REVIEW_FAILED": "📝",
    "QA_FAILED": "❌", "SECURITY_FINDINGS": "⚠️", "BUG_DETECTED": "🐞", "BUG_FIXED": "🔧", "TESTS_PASSED": "🧪",
    "STAGING_DEPLOYMENT": "📦", "VALIDATION_RESULT": "🔎", "TASK_BLOCKED": "⛔", "AGENT_CRASH": "💥",
    "SYSTEM_PAUSED": "⏸️", "SYSTEM_RESUMED": "▶️", "SYSTEM_STOPPED": "⏹️", "PROJECT_COMPLETED": "🏁",
    "APPROVAL_REQUIRED": "🙋", "SYSTEM_ERROR": "🚨",
}


def format_message(title: str, fields: dict[str, Any] | None = None, lines: list[str] | None = None, footer: str | None = None, emoji: str = "") -> str:
    def esc(value: Any) -> str:
        return html.escape(redact(str(value)), quote=False)

    parts = [f"{emoji} <b>{esc(title)}</b>".strip(), ""]
    for key, value in (fields or {}).items():
        if value is None or value == "":
            continue
        parts.append(f"{esc(key)}: {esc(value)}")
    if lines:
        parts.append("")
        parts.extend(f"- {esc(line)}" for line in lines[:12])
        if len(lines) > 12:
            parts.append(f"... and {len(lines) - 12} more")
    if footer:
        parts.extend(["", esc(footer)])
    text = "\n".join(parts).strip()
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 20] + "\n... (truncated)"
    return text


class TelegramNotifier:
    def __init__(
        self,
        secrets: Secrets,
        state: StateManager,
        project_id: str,
        project_name: str,
        dedupe_seconds: int = 600,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        api_base: str = API_BASE,
    ):
        self.secrets = secrets
        self.state = state
        self.project_id = project_id
        self.project_name = project_name
        self.dedupe_seconds = dedupe_seconds
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(20.0)))
        self.api_base = api_base.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.secrets.telegram_enabled and self.secrets.telegram_bot_token and self.secrets.telegram_chat_id)

    def notify(
        self,
        event_type: str,
        title: str,
        fields: dict[str, Any] | None = None,
        lines: list[str] | None = None,
        footer: str | None = None,
        dedupe_key: str | None = None,
    ) -> int | None:
        """Queue a milestone notification. Returns the notification id, or None if deduplicated."""
        all_fields = {"Project": self.project_name, **(fields or {})}
        text = format_message(title, all_fields, lines, footer, EMOJI.get(event_type, ""))
        key = dedupe_key or fingerprint(event_type, text)
        status = "PENDING" if self.enabled else "DISABLED"
        notification_id = self.state.enqueue_notification(self.project_id, event_type, text, key, self.dedupe_seconds, status)
        if notification_id is None:
            log.debug("duplicate notification suppressed: %s", event_type)
        elif not self.enabled:
            log.info("Telegram disabled; notification recorded only: %s", title)
        return notification_id

    async def _send(self, client: httpx.AsyncClient, text: str) -> tuple[bool, bool, str | None, float]:
        """Returns (ok, retryable, error, retry_after_seconds)."""
        url = f"{self.api_base}/bot{self.secrets.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.secrets.telegram_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            response = await client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            return False, True, f"network error: {type(exc).__name__}", 0.0
        if response.status_code == 200:
            return True, False, None, 0.0
        try:
            body = response.json()
        except ValueError:
            body = {}
        description = redact(str(body.get("description", response.text[:200])))
        if response.status_code == 429:
            retry_after = float(body.get("parameters", {}).get("retry_after", 5))
            return False, True, f"rate limited: {description}", retry_after
        if response.status_code >= 500:
            return False, True, f"telegram server error {response.status_code}", 0.0
        if response.status_code == 400 and "parse" in description.lower():
            # Formatting problem: resend as plain text once.
            plain = {**payload}
            plain.pop("parse_mode")
            plain["text"] = html.unescape(text.replace("<b>", "").replace("</b>", ""))
            try:
                retry = await client.post(url, json=plain)
                if retry.status_code == 200:
                    return True, False, None, 0.0
            except httpx.HTTPError:
                pass
        return False, False, f"HTTP {response.status_code}: {description}", 0.0

    async def flush(self, limit: int = 20) -> int:
        """Send queued notifications. Returns the number sent."""
        if not self.enabled:
            return 0
        pending = self.state.pending_notifications(limit, MAX_ATTEMPTS)
        if not pending:
            return 0
        sent = 0
        async with self.client_factory() as client:
            for item in pending:
                ok, retryable, error, retry_after = False, True, None, 0.0
                for attempt in range(3):
                    ok, retryable, error, retry_after = await self._send(client, item["message"])
                    if ok or not retryable:
                        break
                    await asyncio.sleep(max(retry_after, 1.5 * 2**attempt))
                if ok:
                    self.state.mark_notification(item["id"], "SENT")
                    sent += 1
                else:
                    status = "RETRY" if retryable and item["attempts"] + 1 < MAX_ATTEMPTS else "FAILED"
                    self.state.mark_notification(item["id"], status, error)
                    log.warning("telegram notification %s %s: %s", item["id"], status, error)
                await asyncio.sleep(0.05)  # stay well below Telegram's per-chat rate limit
        return sent

    async def run_worker(self, stop: asyncio.Event, interval: float = 2.0) -> None:
        while not stop.is_set():
            try:
                await self.flush()
            except Exception:
                log.exception("telegram flush failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        try:
            await self.flush(limit=50)
        except Exception:
            log.exception("final telegram flush failed")
