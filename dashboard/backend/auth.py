"""Local dashboard authentication.

* bcrypt password hash (``DASHBOARD_PASSWORD_HASH``; set it with ``python -m backend.manage set-password``)
* signed JWT session in an HttpOnly, SameSite=Strict cookie
* CSRF protection: state-changing requests must send ``X-CSRF-Token`` matching the session
* login rate limiting per client address
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException, Request, WebSocket

SESSION_COOKIE = "autodev_session"
CSRF_COOKIE = "autodev_csrf"
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("use a password of at least 10 characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


class AuthManager:
    def __init__(self, username: str, password_hash: str | None, secret_key: str, session_hours: int):
        self.username = username
        self.password_hash = password_hash
        self.secret_key = secret_key
        self.session_seconds = session_hours * 3600
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._revoked: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)

    def _rate_limited(self, client: str) -> bool:
        attempts = self._attempts[client]
        now = time.time()
        while attempts and now - attempts[0] > WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= MAX_ATTEMPTS

    def login(self, client: str, username: str, password: str) -> tuple[str, str]:
        if not self.configured:
            raise HTTPException(503, "dashboard password not configured: run 'python -m backend.manage set-password' in the dashboard folder")
        if self._rate_limited(client):
            raise HTTPException(429, "too many login attempts; try again later")
        valid = secrets.compare_digest(username, self.username) and bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("ascii"))  # type: ignore[union-attr]
        if not valid:
            self._attempts[client].append(time.time())
            raise HTTPException(401, "invalid username or password")
        self._attempts.pop(client, None)
        csrf = secrets.token_urlsafe(24)
        now = int(time.time())
        token = jwt.encode({"sub": username, "iat": now, "exp": now + self.session_seconds, "jti": secrets.token_hex(12), "csrf": csrf}, self.secret_key, algorithm="HS256")
        return token, csrf

    def logout(self, token: str | None) -> None:
        claims = self.decode(token)
        if claims:
            self._revoked.add(claims["jti"])

    def decode(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        try:
            claims = jwt.decode(token, self.secret_key, algorithms=["HS256"], options={"require": ["exp", "sub", "jti", "csrf"]})
        except jwt.PyJWTError:
            return None
        if claims["jti"] in self._revoked or claims["sub"] != self.username:
            return None
        return claims

    def require(self, request: Request) -> dict[str, Any]:
        if not self.configured:
            raise HTTPException(503, "dashboard password not configured: run 'python -m backend.manage set-password' in the dashboard folder")
        claims = self.decode(request.cookies.get(SESSION_COOKIE))
        if claims is None:
            raise HTTPException(401, "authentication required")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            header = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(header, claims["csrf"]):
                raise HTTPException(403, "missing or invalid CSRF token")
        return claims

    def require_ws(self, websocket: WebSocket) -> dict[str, Any] | None:
        if not self.configured:
            return None
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "")
        if origin and origin.split("://", 1)[-1] != host:
            return None  # cross-site WebSocket hijacking protection
        return self.decode(websocket.cookies.get(SESSION_COOKIE))
