"""Durable bridge requests and native-agent progress, outside project sources."""

import contextlib
import json
import os
import sqlite3
import time
import uuid
import zlib
from pathlib import Path


def pack(value):
    return zlib.compress(json.dumps(value, ensure_ascii=False).encode())


def unpack(value):
    return json.loads(zlib.decompress(value)) if value else None


class RequestStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_requests (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    parent_id TEXT, status TEXT NOT NULL, owner INTEGER NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    payload BLOB NOT NULL, response BLOB, submission_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS pending_models
                    ON model_requests(status, thread_id, created);
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, cwd TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle', turn_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL, method TEXT NOT NULL,
                    created REAL NOT NULL, payload BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS thread_events ON events(thread_id, seq);
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_request(self, payload, thread_id, parent_id=None):
        request_id = "req_" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT count(*) FROM model_requests WHERE status='pending'"
                ).fetchone()[0]
                >= 32
            ):
                raise ValueError("The bridge already has 32 pending model requests")
            now = time.time()
            db.execute(
                "INSERT INTO model_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    thread_id,
                    parent_id,
                    "pending",
                    os.getpid(),
                    now,
                    now,
                    pack(payload),
                    None,
                    None,
                ),
            )
        return request_id

    def request(self, request_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM model_requests WHERE id=?", (request_id,)
            ).fetchone()
        if not row:
            raise ValueError("Unknown model request")
        value = dict(row)
        value["payload"] = unpack(row["payload"])
        value["response"] = unpack(row["response"])
        return value

    def pending(self, thread_id=None):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,thread_id,parent_id,created FROM model_requests WHERE status='pending' "
                "AND (? IS NULL OR thread_id=? OR parent_id=?) ORDER BY created LIMIT 32",
                (thread_id, thread_id, thread_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def answer(self, request_id, response, submission_hash):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,response,submission_hash FROM model_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if not row:
                raise ValueError("Unknown model request")
            if (
                row["status"] in ("answered", "delivered")
                and row["submission_hash"] == submission_hash
            ):
                return unpack(row["response"])
            if row["status"] != "pending":
                raise ValueError(
                    f"Request is {row['status']}; it cannot accept a different response"
                )
            db.execute(
                "UPDATE model_requests SET status='answered',response=?,submission_hash=?,updated=? WHERE id=?",
                (pack(response), submission_hash, time.time(), request_id),
            )
        return response

    def set_status(self, request_id, status):
        with self.connect() as db:
            db.execute(
                "UPDATE model_requests SET status=?,updated=? WHERE id=? AND status!='delivered'",
                (status, time.time(), request_id),
            )

    def cancel_pending(self, thread_id=None):
        with self.connect() as db:
            db.execute(
                "UPDATE model_requests SET status='cancelled',updated=? WHERE status='pending' "
                "AND owner=? AND (? IS NULL OR thread_id=? OR parent_id=?)",
                (time.time(), os.getpid(), thread_id, thread_id, thread_id),
            )

    def recover_dead_requests(self):
        with self.connect() as db:
            owners = db.execute(
                "SELECT DISTINCT owner FROM model_requests WHERE status IN ('pending','answered')"
            ).fetchall()
            for owner in owners:
                try:
                    os.kill(owner[0], 0)
                except ProcessLookupError:
                    db.execute(
                        "UPDATE model_requests SET status='cancelled',updated=? WHERE owner=? AND status IN ('pending','answered')",
                        (time.time(), owner[0]),
                    )

    def register_thread(self, thread_id, cwd):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO threads(id,cwd,created,status) VALUES (?,?,?,'idle')",
                (thread_id, cwd, time.time()),
            )

    def thread(self, thread_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
        if not row:
            raise ValueError(
                "Unknown bridge thread; use start_agent or list_agent_tasks"
            )
        return dict(row)

    def threads(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM threads ORDER BY created DESC LIMIT 100"
                )
            ]

    def event(self, method, params):
        thread_id = params.get("threadId") or params.get("thread", {}).get("id")
        if not thread_id:
            return
        payload = dict(params)
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode()) > 32768:
            payload = {"preview": raw[:8000], "truncated": True}
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(thread_id,method,created,payload) VALUES (?,?,?,?)",
                (thread_id, method, time.time(), pack(payload)),
            )
            if method == "turn/started":
                db.execute(
                    "UPDATE threads SET status='active',turn_id=? WHERE id=?",
                    (params.get("turn", {}).get("id"), thread_id),
                )
            elif method == "turn/completed":
                status = params.get("turn", {}).get("status", "completed")
                db.execute(
                    "UPDATE threads SET status=?,turn_id=NULL WHERE id=?",
                    (status, thread_id),
                )

    def events(self, thread_id, after=0, limit=30):
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("after must be nonnegative and limit must be 1..100")
        with self.connect() as db:
            rows = db.execute(
                "SELECT seq,method,created,payload FROM events WHERE thread_id=? AND seq>? ORDER BY seq LIMIT ?",
                (thread_id, after, limit),
            ).fetchall()
        return [{**dict(row), "payload": unpack(row["payload"])} for row in rows]
