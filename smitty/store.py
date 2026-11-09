"""SQLite activity store (~/.smitty/smitty.db).

Smitty records what it does here; the dashboard (a separate process or the same one) shows it.
  events(id, ts, kind, title, detail-json)             append-only activity log
  kv(key, value-json, updated)                         latest snapshot values (state, status, sessions, ...)
  steps(id, ts, session, kind, text, detail-json)      what each session did, step by step (reasoning,
                                                       tool calls, results); "session" is the worker key
  notices(id, ts, kind, title, body, session, urgent, read, detail-json)
                                                       the notification inbox; read is NULL until seen

Writers call ``record()`` / ``put()``; both are no-ops until ``init()`` has run and never raise,
so instrumentation can't break the voice loop. WAL mode lets the dashboard read while Smitty writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    kind   TEXT NOT NULL,
    title  TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    session TEXT NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS steps_session ON steps(session, id);
CREATE TABLE IF NOT EXISTS notices (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    title   TEXT NOT NULL,
    body    TEXT NOT NULL DEFAULT '',
    session TEXT,
    urgent  INTEGER NOT NULL DEFAULT 0,
    read    REAL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS notices_read ON notices(read, id);
"""

STEP_TEXT_MAX = 6000

_db: sqlite3.Connection | None = None
_lock = threading.Lock()


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path).expanduser()
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=2.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        db.commit()
    db.row_factory = sqlite3.Row
    return db


def init(path: Path) -> None:
    """Open the store for writing. Safe to call more than once."""
    global _db
    with _lock:
        if _db is None:
            try:
                _db = connect(path)
                log.info("activity store: %s", Path(path).expanduser())
            except Exception as e:
                log.warning("activity store unavailable (%s); continuing without it", e)


def record(kind: str, title: str, **detail: Any) -> None:
    """Append one activity event. Never raises."""
    if _db is None:
        return
    try:
        with _lock:
            cap = 4000 if kind in ("heard", "said") else 300   # chat lines are shown whole (dashboard, phone)
            _db.execute("INSERT INTO events (ts, kind, title, detail) VALUES (?, ?, ?, ?)",
                        (time.time(), kind, str(title)[:cap], json.dumps(detail, default=str)[:4000]))
            _db.commit()
    except Exception as e:
        log.debug("store.record failed: %s", e)


def put(key: str, value: Any) -> None:
    """Set a snapshot value (latest state, status, sessions...). Never raises."""
    if _db is None:
        return
    try:
        with _lock:
            _db.execute("INSERT INTO kv (key, value, updated) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
                        (key, json.dumps(value, default=str), time.time()))
            _db.commit()
    except Exception as e:
        log.debug("store.put failed: %s", e)


def get(key: str, default: Any = None) -> Any:
    """Read a snapshot value back. Never raises."""
    if _db is None:
        return default
    try:
        with _lock:
            r = _db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default
    except Exception:
        return default


def step(session: str, kind: str, text: str, **detail: Any) -> None:
    """Append one step of a session's work (prompt, thinking, text, tool, tool_result, agent, result, error)."""
    if _db is None:
        return
    try:
        with _lock:
            _db.execute("INSERT INTO steps (ts, session, kind, text, detail) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), session, kind, str(text)[:STEP_TEXT_MAX], json.dumps(detail, default=str)[:2000]))
            _db.commit()
    except Exception as e:
        log.debug("store.step failed: %s", e)


def notice(kind: str, title: str, body: str = "", session: str | None = None, urgent: bool = False, **detail: Any) -> int | None:
    """Put a notification in the inbox. Returns its id."""
    if _db is None:
        return None
    try:
        with _lock:
            cur = _db.execute("INSERT INTO notices (ts, kind, title, body, session, urgent, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (time.time(), kind, str(title)[:200], str(body)[:2000], session, int(urgent),
                               json.dumps(detail, default=str)[:4000]))
            _db.commit()
            return cur.lastrowid
    except Exception as e:
        log.debug("store.notice failed: %s", e)
        return None


def notice_row(r: sqlite3.Row) -> dict[str, Any]:
    try:
        detail = json.loads(r["detail"])
    except Exception:
        detail = {}
    return {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "title": r["title"], "body": r["body"],
            "session": r["session"], "urgent": bool(r["urgent"]), "read": r["read"], "detail": detail}


def notices(unread_only: bool = False, limit: int = 40) -> list[dict[str, Any]]:
    if _db is None:
        return []
    try:
        with _lock:
            q = "SELECT * FROM notices " + ("WHERE read IS NULL " if unread_only else "") + "ORDER BY id DESC LIMIT ?"
            return [notice_row(r) for r in _db.execute(q, (limit,)).fetchall()]
    except Exception:
        return []


def get_notice(notice_id: int) -> dict[str, Any] | None:
    if _db is None:
        return None
    try:
        with _lock:
            r = _db.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
        return notice_row(r) if r else None
    except Exception:
        return None


def mark_read(ids: list[int] | None = None, db: sqlite3.Connection | None = None) -> int:
    """Mark notices read (all unread ones when ids is None). ``db`` lets the dashboard use its own connection."""
    conn = db or _db
    if conn is None:
        return 0
    try:
        with _lock:
            if ids is None:
                cur = conn.execute("UPDATE notices SET read = ? WHERE read IS NULL", (time.time(),))
            else:
                cur = conn.execute(f"UPDATE notices SET read = ? WHERE read IS NULL AND id IN ({','.join('?' * len(ids))})",
                                   (time.time(), *ids))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        log.debug("store.mark_read failed: %s", e)
        return 0


def prune(keep_days: int = 60, steps_days: int = 21) -> None:
    if _db is None:
        return
    try:
        with _lock:
            now = time.time()
            _db.execute("DELETE FROM events WHERE ts < ?", (now - keep_days * 86400,))
            _db.execute("DELETE FROM steps WHERE ts < ?", (now - steps_days * 86400,))
            _db.execute("DELETE FROM notices WHERE ts < ? AND read IS NOT NULL", (now - keep_days * 86400,))
            _db.execute("DELETE FROM kv WHERE key LIKE 'live:%' AND updated < ?", (now - 3600,))
            _db.commit()
    except Exception:
        pass
