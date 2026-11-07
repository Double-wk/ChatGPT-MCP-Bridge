"""Persistent, hashed OAuth credentials for the ChatGPT MCP gateway.

Public OAuth clients; SQLite transactions atomically rotate refresh tokens.
Only random-token SHA-256 digests are stored. Never expose the database in Git.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from pathlib import Path

ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("OAuth database must not be a symbolic link")
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)
        permissions = stat.S_IMODE(self.path.stat().st_mode)
        if permissions & 0o077:
            raise PermissionError("OAuth database must be owner-only (chmod 600)")
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS access_tokens (
                digest TEXT PRIMARY KEY, client_id TEXT NOT NULL, expires INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS refresh_tokens (
                digest TEXT PRIMARY KEY, client_id TEXT NOT NULL, expires INTEGER NOT NULL
            )""")
            for table in ("access_tokens", "refresh_tokens"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "resource" not in columns:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN resource TEXT NOT NULL DEFAULT ''"
                    )
            db.execute("""CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY, redirect_uris TEXT NOT NULL,
                created INTEGER NOT NULL
            )""")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def register_client(self, redirect_uris: list[str]) -> dict:
        client_id = "chatgpt-" + secrets.token_urlsafe(24)
        created = int(time.time())
        with self._connect() as db:
            db.execute(
                "INSERT INTO oauth_clients VALUES (?, ?, ?)",
                (client_id, json.dumps(redirect_uris), created),
            )
        return {"client_id": client_id, "client_id_issued_at": created}

    def known_client(self, client_id: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM oauth_clients WHERE client_id = ?", (client_id,)
                ).fetchone()
                is not None
            )

    def client_redirect_allowed(self, client_id: str, redirect_uri: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT redirect_uris FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        return row is not None and redirect_uri in json.loads(row[0])

    def issue(self, client_id: str, resource: str = "") -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM access_tokens WHERE expires <= ?", (now,))
            db.execute("DELETE FROM refresh_tokens WHERE expires <= ?", (now,))
            db.execute(
                "INSERT INTO access_tokens VALUES (?, ?, ?, ?)",
                (digest(access), client_id, now + ACCESS_TTL_SECONDS, resource),
            )
            db.execute(
                "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?)",
                (digest(refresh), client_id, now + REFRESH_TTL_SECONDS, resource),
            )
        return self._response(access, refresh)

    def authorized(self, access: str, resource: str | None = None) -> bool:
        if not access:
            return False
        with self._connect() as db:
            result = db.execute(
                "SELECT resource FROM access_tokens WHERE digest = ? AND expires > ?",
                (digest(access), int(time.time())),
            ).fetchone()
        return result is not None and (resource is None or result[0] == resource)

    def refresh(
        self, refresh: str, client_id: str, resource: str | None = None
    ) -> dict | None:
        if not refresh or not client_id:
            return None
        now = int(time.time())
        new_access, new_refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        # The previous refresh token is consumed atomically. A replay or a
        # concurrent duplicate gets invalid_grant rather than another token.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires, resource FROM refresh_tokens WHERE digest = ? AND client_id = ?",
                (digest(refresh), client_id),
            ).fetchone()
            if (
                row is None
                or row[0] <= now
                or (resource is not None and row[1] != resource)
            ):
                return None
            remaining_lifetime = row[0]
            db.execute(
                "DELETE FROM refresh_tokens WHERE digest = ?", (digest(refresh),)
            )
            db.execute(
                "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?)",
                (digest(new_refresh), client_id, remaining_lifetime, row[1]),
            )
            db.execute(
                "INSERT INTO access_tokens VALUES (?, ?, ?, ?)",
                (digest(new_access), client_id, now + ACCESS_TTL_SECONDS, row[1]),
            )
            db.execute("DELETE FROM access_tokens WHERE expires <= ?", (now,))
            db.execute("DELETE FROM refresh_tokens WHERE expires <= ?", (now,))
        return self._response(new_access, new_refresh)

    @staticmethod
    def _response(access: str, refresh: str) -> dict:
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": "mcp offline_access",
        }
