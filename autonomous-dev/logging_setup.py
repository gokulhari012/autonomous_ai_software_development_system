"""Structured logging with rotating files and secret redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path
from typing import Iterable

# Patterns for credentials that may show up in tool output or exceptions.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b(\s*[:=]\s*)(['\"]?)[^\s'\",;]{4,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

_known_secret_values: set[str] = set()


def register_secret_values(values: Iterable[str]) -> None:
    for value in values:
        if value and len(value) >= 8:
            _known_secret_values.add(value)


def redact(text: str) -> str:
    """Remove known secret values and credential-looking strings from ``text``."""
    if not text:
        return text
    for value in _known_secret_values:
        text = text.replace(value, "[REDACTED]")

    def _kv(match: re.Match[str]) -> str:
        if match.lastindex and match.lastindex >= 3:
            return f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED]"
        return "[REDACTED]"

    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(_kv, text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        record.msg = redact(message)
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("task_id", "agent", "stage", "project_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> None:
    """Configure root logging once: human-readable console + rotating text and JSON files."""
    global _configured
    if _configured:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    redactor = RedactingFilter()

    text_handler = logging.handlers.RotatingFileHandler(
        log_dir / "autonomous-dev.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    text_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    text_handler.addFilter(redactor)
    root.addHandler(text_handler)

    json_handler = logging.handlers.RotatingFileHandler(
        log_dir / "autonomous-dev.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    json_handler.setFormatter(JsonFormatter())
    json_handler.addFilter(redactor)
    root.addHandler(json_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        console_handler.addFilter(redactor)
        root.addHandler(console_handler)

    # Third-party libraries are noisy at INFO.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def tail_log(log_dir: Path, lines: int = 30) -> list[str]:
    path = log_dir / "autonomous-dev.log"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        content = handle.readlines()[-lines:]
    return [redact(line.rstrip("\n")) for line in content]
