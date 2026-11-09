"""Sync agent for the cloud hub (Supabase): the phone and watch talk to the hub, the hub talks to us.

Runs inside Smitty (``smitty run`` / ``serve`` / ``chat``) or on its own (``smitty hub``). Every
few seconds it

  * mirrors the SQLite activity store up: kv snapshots -> ``hub_state``, heard/said events ->
    ``chat_messages``, the inbox -> ``notices`` (including read marks), session steps -> ``steps``;
  * writes a heartbeat (``hub_state.mac``) so the app knows whether the Mac is online;
  * drains the ``commands`` queue in order and performs each one through Smitty's control
    channel (the same token-guarded routes the dashboard uses), recording the outcome.

The schema is in supabase/migrations/0001_hub.sql. Configure with SMITTY_HUB_URL and
SMITTY_HUB_KEY (the project's *service role* key; it never leaves this machine) or with
~/.smitty/hub.json ``{"url": ..., "key": ...}``.

Nothing here may break Smitty: every failure is logged and retried with backoff. Commands that
need Smitty while it is not running stay queued; nothing is lost while the Mac is offline, it
simply waits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from smitty import __version__, store
from smitty.control import HEADER as CONTROL_HEADER
from smitty.control import read_token

log = logging.getLogger(__name__)

CURSOR_FILE = "hub.cursor.json"
CONFIG_FILE = "hub.json"
FIRST_SYNC = {"events": 100, "notices": 50, "steps": 300}     # how much history to push the first time
BATCH = {"events": 200, "notices": 100, "steps": 300}          # per cycle
MAX_BACKOFF_S = 60.0
PRUNE_EVERY_S = 86400.0
SKIP_KV_PREFIXES = ("live:",)       # rewritten several times a second; too chatty for the hub
COMMAND_ROUTES = {"chat": "chat", "approval": "approval", "notice_reply": "notice", "session": "session",
                  "schedule": "schedule", "mode": "mode"}


class HubUnreachable(Exception):
    pass


def load_config(data_dir: Path) -> tuple[str, str] | None:
    """(url, key) from the environment or ~/.smitty/hub.json; None when the hub is not configured."""
    url, key = os.environ.get("SMITTY_HUB_URL", "").strip(), os.environ.get("SMITTY_HUB_KEY", "").strip()
    if not (url and key):
        try:
            cfg = json.loads((Path(data_dir) / CONFIG_FILE).read_text())
            url, key = str(cfg.get("url") or "").strip(), str(cfg.get("key") or "").strip()
        except (OSError, ValueError):
            return None
    if not (url and key):
        return None
    return url.rstrip("/"), key


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


class Hub:
    def __init__(self, url: str, key: str, db_path: Path, data_dir: Path, control_port: int,
                 interval_s: float = 3.0, mode: str = "embedded"):
        self.url, self.key = url.rstrip("/"), key
        self.db_path, self.data_dir = Path(db_path).expanduser(), Path(data_dir).expanduser()
        self.control_port = control_port
        self.interval_s = interval_s
        self.mode = mode
        self.cursor: dict[str, Any] = {}
        self.connected: bool | None = None     # None until the first attempt
        self.stats = {"cycles": 0, "pushed": 0, "commands": 0, "errors": 0}
        self._http: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._backoff = interval_s
        self._last_prune = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._load_cursor()
        self._task = asyncio.create_task(self._run(), name="hub-sync")
        log.info("hub sync -> %s every %.0fs", self.url, self.interval_s)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        try:
            if self._http is not None and self.connected:
                await self._heartbeat(online=False)
        except Exception:
            pass
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._save_cursor()

    async def _run(self) -> None:
        while True:
            try:
                await self.sync_once()
                if self.connected is not True:
                    log.info("hub: connected to %s", self.url)
                self.connected = True
                self._backoff = self.interval_s
            except asyncio.CancelledError:
                raise
            except HubUnreachable as e:
                self.stats["errors"] += 1
                if self.connected is not False:
                    log.warning("hub: unreachable (%s); will keep trying", e)
                self.connected = False
                self._backoff = min(self._backoff * 2, MAX_BACKOFF_S)
            except Exception:
                self.stats["errors"] += 1
                log.exception("hub: sync cycle failed")
                self._backoff = min(self._backoff * 2, MAX_BACKOFF_S)
            await asyncio.sleep(self._backoff if self.connected is False else self.interval_s)

    # ------------------------------------------------------------------ one cycle
    async def sync_once(self) -> None:
        """Push what changed, write the heartbeat, run queued commands. Raises HubUnreachable."""
        self.stats["cycles"] += 1
        db = self._db()
        try:
            if db is not None:
                await self._push_kv(db)
                await self._push_chat(db)
                await self._push_notices(db)
                await self._push_steps(db)
            await self._heartbeat(online=True, db=db)
        finally:
            if db is not None:
                db.close()
        self._save_cursor()
        await self._drain_commands()
        if time.time() - self._last_prune > PRUNE_EVERY_S:
            self._last_prune = time.time()
            try:
                await self._req("POST", "/rest/v1/rpc/hub_prune", json={})
            except HubUnreachable:
                raise
            except Exception as e:
                log.debug("hub: prune failed: %s", e)

    # ------------------------------------------------------------------ mirroring
    def _db(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            return store.connect(self.db_path, readonly=True)
        except sqlite3.Error as e:
            log.debug("hub: cannot open store: %s", e)
            return None

    async def _push_kv(self, db: sqlite3.Connection) -> None:
        since = float(self.cursor.get("kv", 0.0))
        rows = db.execute("SELECT key, value, updated FROM kv WHERE updated > ? ORDER BY updated", (since,)).fetchall()
        rows = [r for r in rows if not r["key"].startswith(SKIP_KV_PREFIXES)]
        if not rows:
            return
        await self._upsert("hub_state", [{"key": r["key"], "value": _loads(r["value"], None), "updated": _iso(r["updated"])}
                                         for r in rows])
        self.cursor["kv"] = max(r["updated"] for r in rows)

    async def _push_chat(self, db: sqlite3.Connection) -> None:
        after = self.cursor.get("events")
        if after is None:      # first sync: only recent history
            rows = list(reversed(db.execute("SELECT * FROM events WHERE kind IN ('heard', 'said') ORDER BY id DESC LIMIT ?",
                                            (FIRST_SYNC["events"],)).fetchall()))
            last = db.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
        else:
            rows = db.execute("SELECT * FROM events WHERE kind IN ('heard', 'said') AND id > ? ORDER BY id LIMIT ?",
                              (int(after), BATCH["events"])).fetchall()
            last = rows[-1]["id"] if len(rows) == BATCH["events"] else (db.execute("SELECT MAX(id) FROM events").fetchone()[0] or after)
        if rows:
            await self._upsert("chat_messages", [{"id": r["id"], "ts": _iso(r["ts"]), "role": r["kind"], "text": r["title"],
                                                  "detail": _loads(r["detail"], {})} for r in rows])
        self.cursor["events"] = int(last)

    async def _push_notices(self, db: sqlite3.Connection) -> None:
        after, read_since = self.cursor.get("notices"), float(self.cursor.get("notices_read", 0.0))
        if after is None:
            rows = list(reversed(db.execute("SELECT * FROM notices ORDER BY id DESC LIMIT ?", (FIRST_SYNC["notices"],)).fetchall()))
            last = db.execute("SELECT MAX(id) FROM notices").fetchone()[0] or 0
            read_since = float(db.execute("SELECT MAX(read) FROM notices").fetchone()[0] or 0.0)   # older read marks are history
        else:
            rows = db.execute("SELECT * FROM notices WHERE id > ? OR (read IS NOT NULL AND read > ?) ORDER BY id LIMIT ?",
                              (int(after), read_since, BATCH["notices"])).fetchall()
            last = max([int(after)] + [r["id"] for r in rows])
        if rows:
            notices = [self._notice(r) for r in rows]
            for n in notices:
                if n["image"]:
                    n["image"] = await self._upload_image(n["image"])
            await self._upsert("notices", notices)
            read_marks = [r["read"] for r in rows if r["read"]]
            if read_marks:
                read_since = max([read_since] + read_marks)
        self.cursor["notices"] = int(last)
        self.cursor["notices_read"] = read_since

    @staticmethod
    def _notice(r: sqlite3.Row) -> dict[str, Any]:
        detail = _loads(r["detail"], {})
        return {"id": r["id"], "ts": _iso(r["ts"]), "kind": r["kind"], "title": r["title"], "body": r["body"],
                "session": r["session"], "urgent": bool(r["urgent"]), "read": _iso(r["read"]), "detail": detail,
                "image": detail.get("image") if isinstance(detail, dict) else None}

    async def _upload_image(self, rel: str) -> str | None:
        """Put a screenshot from ~/.smitty/artifacts/<rel> into the hub's `artifacts` bucket at the same path.

        Returns the object path (what the phone fetches), or None when the file is gone. Idempotent.
        """
        done: list[str] = self.cursor.setdefault("uploaded", [])
        if rel in done:
            return rel
        path = (self.data_dir / "artifacts" / rel)
        try:
            if not path.resolve().is_relative_to((self.data_dir / "artifacts").resolve()) or not path.exists():
                return None
            data = path.read_bytes()
        except OSError:
            return None
        ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}.get(
            path.suffix.lower().lstrip("."), "application/octet-stream")
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": ctype, "x-upsert": "true"}
        try:
            async with self._session().post(f"{self.url}/storage/v1/object/artifacts/{rel}", data=data, headers=headers) as r:
                if r.status >= 400:
                    raise HubUnreachable(f"upload {rel} -> {r.status} {(await r.text())[:200]}")
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            raise HubUnreachable(str(e) or type(e).__name__) from e
        done.append(rel)
        del done[:-500]
        return rel

    async def _push_steps(self, db: sqlite3.Connection) -> None:
        after = self.cursor.get("steps")
        if after is None:
            rows = list(reversed(db.execute("SELECT * FROM steps ORDER BY id DESC LIMIT ?", (FIRST_SYNC["steps"],)).fetchall()))
            last = db.execute("SELECT MAX(id) FROM steps").fetchone()[0] or 0
        else:
            rows = db.execute("SELECT * FROM steps WHERE id > ? ORDER BY id LIMIT ?", (int(after), BATCH["steps"])).fetchall()
            last = rows[-1]["id"] if rows else int(after)
        if rows:
            await self._upsert("steps", [{"id": r["id"], "ts": _iso(r["ts"]), "session": r["session"], "kind": r["kind"],
                                          "text": r["text"][:2000], "detail": _loads(r["detail"], {})} for r in rows])
        self.cursor["steps"] = int(last)

    async def _heartbeat(self, online: bool, db: sqlite3.Connection | None = None) -> None:
        smitty_up, mode = False, None
        if db is not None:
            r = db.execute("SELECT value, updated FROM kv WHERE key = 'status'").fetchone()
            if r and time.time() - r["updated"] < 20:
                smitty_up = True
                mode = (_loads(r["value"], {}) or {}).get("mode")
        now = time.time()
        await self._upsert("hub_state", [{"key": "mac", "updated": _iso(now), "value": {
            "online": online, "seen": _iso(now), "pid": os.getpid(), "version": __version__, "sync": self.mode,
            "smitty": smitty_up and online, "mode": mode}}])

    # ------------------------------------------------------------------ commands
    async def _drain_commands(self) -> None:
        status, rows = await self._req("GET", "/rest/v1/commands", params={"status": "eq.queued", "order": "created.asc", "limit": "20"})
        if status != 200 or not isinstance(rows, list):
            raise HubUnreachable(f"commands query -> {status}")
        for cmd in rows:
            cid = cmd.get("id")
            # Claim it (status=eq.queued guards against two sync agents taking the same row).
            status, claimed = await self._req("PATCH", "/rest/v1/commands", params={"id": f"eq.{cid}", "status": "eq.queued"},
                                              json={"status": "running", "started": _iso(time.time())},
                                              prefer="return=representation")
            if status not in (200, 204) or not claimed:
                continue
            outcome = await self._perform(cmd)
            if outcome is None:              # Smitty is not running: put it back for later
                await self._req("PATCH", "/rest/v1/commands", params={"id": f"eq.{cid}"},
                                json={"status": "queued", "started": None})
                log.info("hub: %s command waits for Smitty to start", cmd.get("kind"))
                break                        # keep the order; nothing after it can run either
            ok, result = outcome
            await self._req("PATCH", "/rest/v1/commands", params={"id": f"eq.{cid}"},
                            json={"status": "done" if ok else "failed", "result": result, "finished": _iso(time.time())})
            self.stats["commands"] += 1
            log.info("hub: %s command from %s -> %s", cmd.get("kind"), cmd.get("device") or "app",
                     "done" if ok else f"failed ({result.get('error')})")

    async def _perform(self, cmd: dict[str, Any]) -> tuple[bool, dict[str, Any]] | None:
        """Run one command. (ok, result), or None when Smitty is not running to take it."""
        kind, body = str(cmd.get("kind") or ""), cmd.get("body") or {}
        if not isinstance(body, dict):
            return False, {"error": "body must be an object"}
        if kind == "mark_read":
            ids = body.get("ids")
            try:
                conn = store.connect(self.db_path)
                try:
                    n = store.mark_read([int(i) for i in ids] if isinstance(ids, list) else None, db=conn)
                finally:
                    conn.close()
            except Exception as e:
                return False, {"error": str(e)}
            return True, {"marked": n}
        route = COMMAND_ROUTES.get(kind)
        if route is None:
            return False, {"error": f"unknown command {kind!r}"}
        token = read_token(self.data_dir)
        if not token:
            return None
        try:
            async with self._session().post(f"http://127.0.0.1:{self.control_port}/control/{route}", json=body,
                                            headers={CONTROL_HEADER: token}, timeout=aiohttp.ClientTimeout(total=60)) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {"error": f"unexpected reply ({r.status})"}
                if r.status == 403:          # stale token from an older Smitty: treat as "not running"
                    return None
                return r.status < 400, data if isinstance(data, dict) else {"reply": data}
        except (aiohttp.ClientError, OSError, TimeoutError):
            return None

    # ------------------------------------------------------------------ http
    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._http

    async def _req(self, method: str, path: str, json: Any = None, params: dict[str, str] | None = None,
                   prefer: str | None = None) -> tuple[int, Any]:
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        try:
            async with self._session().request(method, self.url + path, json=json, params=params, headers=headers) as r:
                text = await r.text()
                if r.status >= 500 or r.status in (401, 403, 404):
                    raise HubUnreachable(f"{method} {path} -> {r.status} {text[:200]}")
                return r.status, (_loads(text, None) if text else None)
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            raise HubUnreachable(str(e) or type(e).__name__) from e

    async def _upsert(self, table: str, rows: list[dict[str, Any]]) -> None:
        status, data = await self._req("POST", f"/rest/v1/{table}", json=rows, prefer="resolution=merge-duplicates,return=minimal")
        if status >= 400:
            raise HubUnreachable(f"upsert {table} -> {status} {str(data)[:200]}")
        self.stats["pushed"] += len(rows)

    # ------------------------------------------------------------------ cursor
    def _load_cursor(self) -> None:
        try:
            self.cursor = json.loads((self.data_dir / CURSOR_FILE).read_text())
            if not isinstance(self.cursor, dict) or self.cursor.get("url") != self.url:
                self.cursor = {}          # a different project: start over
        except (OSError, ValueError):
            self.cursor = {}
        self.cursor["url"] = self.url

    def _save_cursor(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / CURSOR_FILE).write_text(json.dumps(self.cursor))
        except OSError as e:
            log.debug("hub: could not save cursor: %s", e)
