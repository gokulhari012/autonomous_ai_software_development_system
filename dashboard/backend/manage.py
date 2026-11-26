"""Dashboard management CLI.

    python -m backend.manage set-password        # prompts for a password, writes the bcrypt hash to dashboard/.env
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .auth import hash_password
from .settings import DASHBOARD_DIR


def set_password(username: str) -> int:
    password = getpass.getpass("New dashboard password: ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        hashed = hash_password(password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    env_path = DASHBOARD_DIR / ".env"
    lines = [l for l in (env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []) if not l.startswith(("DASHBOARD_PASSWORD_HASH=", "DASHBOARD_USERNAME="))]
    lines += [f"DASHBOARD_USERNAME={username}", f"DASHBOARD_PASSWORD_HASH={hashed}"]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Password stored for user {username!r} in {env_path} (never commit this file).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("set-password")
    p.add_argument("--username", default="admin")
    args = parser.parse_args()
    if args.command == "set-password":
        return set_password(args.username)
    return 1


if __name__ == "__main__":
    sys.exit(main())
