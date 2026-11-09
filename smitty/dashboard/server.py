"""Dashboard server: serves one page and a small JSON API on top of the SQLite store.

    uv run smitty dashboard            -> http://127.0.0.1:8770
    uv run smitty dashboard --demo     -> same page on sample data (separate demo database)

Reading works whether or not Smitty is running: "online" just means Smitty's heartbeat in
the store is fresh. Actions (chat, approvals, stopping a session, removing a schedule...)
are forwarded to the running Smitty over its control channel (see smitty/control.py). The
few that make sense while Smitty is off (remove or pause a schedule, switch the attention
mode, mark notifications read) are applied to the files directly.

Requests are only accepted for 127.0.0.1/localhost, and anything that changes state must
carry the page's own header, so another website open in the browser cannot drive Smitty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from smitty import claude_sessions, store
from smitty.control import HEADER, read_token

log = logging.getLogger(__name__)

INDEX = Path(__file__).with_name("index.html")
ONLINE_WITHIN_S = 15.0
PAGE_HEADER = "X-Smitty-Dashboard"
OFFLINE = "Smitty is not running. Start it to do that."


class Dashboard:
    def __init__(self, db_path: Path, data_dir: Path, host: str = "127.0.0.1", port: int = 8770, demo: bool = False,
                 control_port: int = 8765, tasks_dir: Path | None = None):
        self.db_path, self.data_dir = Path(db_path).expanduser(), Path(data_dir).expanduser()
        self.host, self.port, self.demo = host, port, demo
        self.control_port = control_port
        self.tasks_dir = Path(tasks_dir).expanduser() if tasks_dir else self.data_dir
        self._runner: web.AppRunner | None = None
        self._http: aiohttp.ClientSession | None = None
        self._outside_at, self._outside_rows = 0.0, []
        self.app = web.Application(middlewares=[self._guard], client_max_size=64 * 1024)
        self.app.add_routes([
            web.get("/", self.index),
            web.get("/api/overview", self.overview),
            web.get("/api/events", self.events),
            web.get("/api/chat", self.chat_log),
            web.get("/api/steps", self.steps),
            web.get("/api/notices", self.notices),
            web.get("/api/artifact/{path:.+}", self.artifact),
            web.post("/api/chat", self.chat_send),
            web.post("/api/mode", self.set_mode),
            web.post("/api/volume", self.set_volume),
            web.post("/api/held/clear", self.clear_held),
            web.post("/api/notices/read", self.read_notices),
            web.post("/api/notices/{id}/reply", self.reply_notice),
            web.post("/api/approvals/{id}", self.answer_approval),
            web.post("/api/schedules/{id}/{action}", self.schedule_action),
            web.post("/api/sessions/{key}/{action}", self.session_action),
        ])

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if request.headers.get("Host", "") not in allowed:
            return web.json_response({"error": "not allowed"}, status=403)       # DNS-rebinding guard
        if request.method != "GET":
            origin = request.headers.get("Origin")
            if request.headers.get(PAGE_HEADER) != "1" or (origin and origin.split("://", 1)[-1] not in allowed):
                return web.json_response({"error": "not allowed"}, status=403)   # only this page may act
        return await handler(request)

    async def start(self) -> bool:
        try:
            store.connect(self.db_path).close()        # make sure the tables this version expects exist
        except Exception as e:
            log.debug("dashboard: could not prepare the store: %s", e)
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, self.host, self.port).start()
        except OSError as e:
            log.info("dashboard not started on port %d (%s); probably already running", self.port, e)
            await self._runner.cleanup()
            self._runner = None
            return False
        log.info("dashboard on http://%s:%d", self.host, self.port)
        return True

    async def stop(self) -> None:
        if self._http:
            await self._http.close()
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ talking to the running Smitty
    async def _smitty(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        """Forward an action to Smitty's control channel. None means Smitty cannot be reached."""
        token = None if self.demo else read_token(self.data_dir)
        if not token:
            return None
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        try:
            async with self._http.post(f"http://127.0.0.1:{self.control_port}/control/{path}", json=body,
                                       headers={HEADER: token}) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {"error": f"unexpected reply ({r.status})"}
                return r.status, data
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            log.debug("dashboard: control channel unreachable: %s", e)
            return None

    async def _forward(self, path: str, body: dict[str, Any], offline: str = OFFLINE) -> web.Response:
        if self.demo:
            return web.json_response({"error": "This is sample data. Open the real dashboard to do that."}, status=409)
        res = await self._smitty(path, body)
        if res is None:
            return web.json_response({"error": offline}, status=503)
        return web.json_response(res[1], status=res[0])

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    def _write(self, fn) -> Any:
        """Run fn(db) on a short-lived read-write connection (used only for the offline-safe actions)."""
        db = store.connect(self.db_path)
        try:
            out = fn(db)
            db.commit()
            return out
        finally:
            db.close()

    def _log(self, kind: str, title: str, **detail: Any) -> None:
        try:
            self._write(lambda db: db.execute("INSERT INTO events (ts, kind, title, detail) VALUES (?, ?, ?, ?)",
                                              (time.time(), kind, title[:300], json.dumps(detail))))
        except Exception as e:
            log.debug("dashboard: could not log %r: %s", title, e)

    # ------------------------------------------------------------------ data access
    def _db(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            return store.connect(self.db_path, readonly=True)
        except sqlite3.Error as e:
            log.debug("dashboard: cannot open store: %s", e)
            return None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        try:
            detail = json.loads(r["detail"])
        except Exception:
            detail = {}
        return {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "title": r["title"], "detail": detail}

    def _json_file(self, name: str, default: Any) -> Any:
        p = self.data_dir / name
        try:
            return json.loads(p.read_text()) if p.exists() else default
        except Exception:
            return default

    def _outside(self, kv: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Claude Code sessions open on this Mac that Smitty did not start (terminal, desktop app, IDE).
        Blocking file reads: call it through asyncio.to_thread. ``kv`` needs "status" and "sessions"."""
        if self.demo:
            seeded = kv.get("outside", {}).get("value")
            return seeded if isinstance(seeded, list) else []
        if time.monotonic() - self._outside_at > 1.5:
            hb = kv.get("status")
            online = bool(hb and time.time() - hb["updated"] < ONLINE_WITHIN_S)
            pid = hb["value"].get("pid") if online and isinstance(hb["value"], dict) else None
            own = kv.get("sessions", {}).get("value") if online else None
            ids = {s.get("session_id") for s in own if isinstance(s, dict) and s.get("session_id")} if isinstance(own, list) else set()
            try:
                self._outside_rows = claude_sessions.live_sessions(smitty_pid=pid if isinstance(pid, int) else None, own_ids=ids)
            except Exception as e:
                log.debug("dashboard: could not list Claude Code sessions: %s", e)
                self._outside_rows = []
            self._outside_at = time.monotonic()
        return self._outside_rows

    def _kv(self, *keys: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        db = self._db()
        if db is None:
            return out
        try:
            for r in db.execute(f"SELECT key, value, updated FROM kv WHERE key IN ({','.join('?' * len(keys))})", keys):
                try:
                    out[r["key"]] = {"value": json.loads(r["value"]), "updated": r["updated"]}
                except Exception:
                    pass
        except sqlite3.Error:
            pass
        finally:
            db.close()
        return out

    # ------------------------------------------------------------------ handlers
    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=INDEX.read_text(), content_type="text/html", headers={"Cache-Control": "no-store"})

    async def events(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 120)), 500)
        kind = request.query.get("kind")
        db = self._db()
        if db is None:
            return web.json_response({"events": []})
        try:
            if kind:
                rows = db.execute("SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return web.json_response({"events": [self._row(r) for r in rows]})
        finally:
            db.close()

    @staticmethod
    def _live(db: sqlite3.Connection, key: str, now: float) -> dict[str, Any] | None:
        r = db.execute("SELECT value, updated FROM kv WHERE key = ?", (f"live:{key}",)).fetchone()
        if not r or now - r["updated"] > 60:
            return None
        try:
            v = json.loads(r["value"])
        except Exception:
            return None
        return v if v.get("kind") else None

    async def chat_log(self, request: web.Request) -> web.Response:
        """The conversation (spoken and typed) plus the reply being written right now."""
        limit = min(int(request.query.get("limit", 60)), 200)
        out: dict[str, Any] = {"messages": [], "busy": False, "live": None}
        db = self._db()
        if db is None:
            return web.json_response(out)
        try:
            rows = db.execute("SELECT * FROM events WHERE kind IN ('heard', 'said') ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out["messages"] = [self._row(r) for r in reversed(rows)]
            now = time.time()
            hb = db.execute("SELECT updated FROM kv WHERE key = 'status'").fetchone()
            online = bool(hb and now - hb["updated"] < ONLINE_WITHIN_S)
            c = db.execute("SELECT value FROM kv WHERE key = 'chat'").fetchone()
            out["busy"] = bool(online and c and json.loads(c["value"]).get("busy"))
            if out["busy"]:
                out["live"] = self._live(db, "brain", now)
        except Exception as e:
            log.debug("dashboard: chat log failed: %s", e)
        finally:
            db.close()
        return web.json_response(out)

    async def steps(self, request: web.Request) -> web.Response:
        """What one session did, step by step, after a given step id; plus the text being written now."""
        key = request.query.get("session", "")
        after = int(request.query.get("after", 0) or 0)
        limit = min(int(request.query.get("limit", 400)), 1000)
        out: dict[str, Any] = {"steps": [], "live": None, "session": None}
        outside_id = None if self.demo else claude_sessions.session_id(key)
        if outside_id:      # a Claude Code session Smitty did not start: its steps come from its own transcript
            rows = await asyncio.to_thread(self._outside, self._kv("status", "sessions"))
            out["session"] = next((r for r in rows if r.get("key") == key), None)
            got = await asyncio.to_thread(claude_sessions.read_steps, outside_id, after, limit,
                                          (out["session"] or {}).get("project"))
            out["steps"], out["after"] = got["steps"], got["after"]
            return web.json_response(out)
        db = self._db()
        if db is None or not key:
            return web.json_response(out)
        try:
            if after:
                rows = db.execute("SELECT * FROM steps WHERE session = ? AND id > ? ORDER BY id LIMIT ?", (key, after, limit)).fetchall()
            else:   # first load: the most recent steps
                rows = list(reversed(db.execute("SELECT * FROM steps WHERE session = ? ORDER BY id DESC LIMIT ?", (key, limit)).fetchall()))
            for r in rows:
                try:
                    detail = json.loads(r["detail"])
                except Exception:
                    detail = {}
                out["steps"].append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "text": r["text"], "detail": detail})
            now = time.time()
            out["live"] = self._live(db, key, now)
            hb = db.execute("SELECT updated FROM kv WHERE key = 'status'").fetchone()
            if hb and now - hb["updated"] < ONLINE_WITHIN_S:
                r = db.execute("SELECT value FROM kv WHERE key = 'sessions'").fetchone()
                for x in (json.loads(r["value"]) if r else []):
                    if x.get("key") == key:
                        out["session"] = x
                if self.demo and out["session"] is None:
                    r = db.execute("SELECT value FROM kv WHERE key = 'outside'").fetchone()
                    out["session"] = next((x for x in (json.loads(r["value"]) if r else []) if x.get("key") == key), None)
        except Exception as e:
            log.debug("dashboard: steps failed: %s", e)
        finally:
            db.close()
        return web.json_response(out)

    async def notices(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 30)), 100)
        db = self._db()
        if db is None:
            return web.json_response({"notices": [], "unread": 0})
        try:
            rows = db.execute("SELECT * FROM notices ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            unread = db.execute("SELECT COUNT(*) FROM notices WHERE read IS NULL").fetchone()[0]
            return web.json_response({"notices": [store.notice_row(r) for r in rows], "unread": unread})
        except sqlite3.Error:
            return web.json_response({"notices": [], "unread": 0})
        finally:
            db.close()

    async def artifact(self, request: web.Request) -> web.Response:
        """A screenshot from ~/.smitty/artifacts (review notices refer to them by relative path)."""
        root = (self.data_dir / "artifacts").resolve()
        path = (root / request.match_info["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------------ actions
    async def chat_send(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        return await self._forward("chat", {"text": body.get("text")}, offline="Smitty is not running. Start it to chat.")

    async def reply_notice(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        return await self._forward("notice", {"id": request.match_info["id"], "text": body.get("text")},
                                   offline="Smitty is not running. Start it and your reply can continue the work.")

    async def answer_approval(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        return await self._forward("approval", {"id": request.match_info["id"], "answer": body.get("answer")})

    async def session_action(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        return await self._forward("session", {"key": request.match_info["key"], "action": request.match_info["action"],
                                               "text": body.get("text")})

    async def read_notices(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        ids = body.get("ids")
        ids = [int(i) for i in ids] if isinstance(ids, list) else None
        n = self._write(lambda db: store.mark_read(ids, db=db))
        return web.json_response({"ok": True, "marked": n})

    async def set_mode(self, request: web.Request) -> web.Response:
        mode = str((await self._body(request)).get("mode") or "")
        if mode not in ("active", "passive"):
            return web.json_response({"error": "mode must be active or passive"}, status=400)
        res = await self._smitty("mode", {"mode": mode})
        if res is not None:
            return web.json_response(res[1], status=res[0])
        # Smitty is off: remember the choice so it applies the next time it starts.
        self._write(lambda db: db.execute(
            "INSERT INTO kv (key, value, updated) VALUES ('attention', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated", (json.dumps(mode), time.time())))
        self._log("system", f"Switched to {mode} mode", attention=mode, via="dashboard")
        return web.json_response({"ok": True, "mode": mode})

    async def set_volume(self, request: web.Request) -> web.Response:
        try:
            value = round(min(1.0, max(0.0, float((await self._body(request)).get("volume")))), 2)
        except (TypeError, ValueError):
            return web.json_response({"error": "volume must be a number from 0 to 1"}, status=400)
        res = await self._smitty("volume", {"volume": value})
        if res is not None:
            return web.json_response(res[1], status=res[0])
        # Smitty is off: save it so the next start uses it.
        self._write(lambda db: db.execute(
            "INSERT INTO kv (key, value, updated) VALUES ('volume', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated", (json.dumps(value), time.time())))
        return web.json_response({"ok": True, "volume": value})

    async def clear_held(self, request: web.Request) -> web.Response:
        res = await self._smitty("held", {})
        if res is not None:
            return web.json_response(res[1], status=res[0])
        n = len(self._json_file("missed.json", []))
        (self.data_dir / "missed.json").write_text("[]")
        if n:
            self._log("schedule", f"Discarded {n} held result(s)", outcome="discarded", via="dashboard")
        return web.json_response({"ok": True, "discarded": n})

    async def schedule_action(self, request: web.Request) -> web.Response:
        sid, action = request.match_info["id"], request.match_info["action"]
        if action not in ("remove", "pause", "resume", "run"):
            return web.json_response({"error": "unknown action"}, status=400)
        res = await self._smitty("schedule", {"id": sid, "action": action})
        if res is not None:
            return web.json_response(res[1], status=res[0])
        if action == "run":
            return web.json_response({"error": "Smitty is not running. Start it to run a job now."}, status=503)
        # Smitty is off, so nothing else is using schedules.json: change the file directly.
        from smitty.scheduler import Scheduler
        sch = Scheduler(None, self.tasks_dir, self.data_dir / "schedules.json")
        try:
            s = sch.remove(sid) if action == "remove" else sch.set_enabled(sid, action == "resume")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if s is None:
            return web.json_response({"error": "no such schedule"}, status=400)
        self._log("schedule", f"{ {'remove': 'Removed', 'pause': 'Paused', 'resume': 'Resumed'}[action] }: {s.title}",
                  outcome={"remove": "cancelled", "pause": "paused", "resume": "resumed"}[action], via="dashboard")
        return web.json_response({"ok": True, "title": s.title})

    async def overview(self, request: web.Request) -> web.Response:
        now = time.time()
        out: dict[str, Any] = {"now": now, "demo": self.demo, "online": False, "state": "offline", "status": {},
                               "stats": {}, "hours": [], "sessions": [], "outside": [], "schedules": [], "device": {}, "brain": {},
                               "systems": [], "last_heard": None, "last_said": None, "held": 0,
                               "attention": "active", "approvals": [], "unread": 0, "can_act": False, "chat_busy": False}
        db = self._db()
        kv: dict[str, dict[str, Any]] = {}
        if db is not None:
            try:
                for r in db.execute("SELECT key, value, updated FROM kv"):
                    try:
                        kv[r["key"]] = {"value": json.loads(r["value"]), "updated": r["updated"]}
                    except Exception:
                        pass
                midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                today = [self._row(r) for r in db.execute("SELECT * FROM events WHERE ts >= ? ORDER BY id", (midnight,))]
                out["stats"] = self._stats(today)
                out["hours"] = self._hours(db, now)
                for kind, key in (("heard", "last_heard"), ("said", "last_said")):
                    r = db.execute("SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
                    out[key] = self._row(r) if r else None
                out["total_events"] = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                try:
                    out["unread"] = db.execute("SELECT COUNT(*) FROM notices WHERE read IS NULL").fetchone()[0]
                except sqlite3.Error:
                    pass
            finally:
                db.close()

        hb = kv.get("status")
        out["online"] = bool(hb and now - hb["updated"] < ONLINE_WITHIN_S)
        out["status"] = hb["value"] if hb else {}
        out["last_seen"] = hb["updated"] if hb else None
        out["state"] = (kv.get("state", {}).get("value") or "idle") if out["online"] else "offline"
        out["sessions"] = kv.get("sessions", {}).get("value", []) if out["online"] else []
        out["outside"] = await asyncio.to_thread(self._outside, kv)
        out["device"] = kv.get("device", {}).get("value", {})
        out["brain"] = kv.get("brain", {}).get("value", {})
        out["systems"] = kv.get("systems", {}).get("value", [])
        out["attention"] = kv.get("attention", {}).get("value") or "active"
        saved_volume = kv.get("volume", {}).get("value")
        out["volume"] = saved_volume if isinstance(saved_volume, (int, float)) else out["status"].get("volume", 0.5)
        out["approvals"] = kv.get("approvals", {}).get("value", []) if out["online"] else []
        out["chat_busy"] = bool(out["online"] and (kv.get("chat", {}).get("value") or {}).get("busy"))
        # Chat, approvals and session actions need a running Smitty that has opened its control channel.
        out["can_act"] = bool(out["online"] and not self.demo and out["status"].get("control") and read_token(self.data_dir))
        sched = self._json_file("schedules.json", [])
        out["schedules"] = sorted(
            [{"id": s.get("id"), "title": s.get("title"), "kind": s.get("kind"), "every_minutes": s.get("every_minutes"),
              "at": s.get("at"), "run_at": s.get("run_at"), "next_run": s.get("next_run"), "last_run": s.get("last_run"),
              "enabled": s.get("enabled", True), "prompt": (s.get("prompt") or "")[:400]} for s in sched],
            key=lambda s: (not s["enabled"], s["next_run"] or "9999"))
        out["held"] = len(self._json_file("missed.json", []))
        return web.json_response(out)

    # ------------------------------------------------------------------ aggregation
    @staticmethod
    def _stats(today: list[dict[str, Any]]) -> dict[str, int]:
        s = dict(conversations=0, replies=0, announcements=0, wakes=0, wakes_ignored=0, sessions_started=0,
                 sessions_finished=0, session_errors=0, checks_silent=0, checks_spoke=0, checks_held=0,
                 approvals_yes=0, approvals_no=0, hooks=0)
        for e in today:
            k, d = e["kind"], e["detail"]
            if k == "heard":
                s["conversations"] += 1
            elif k == "said":
                s["announcements" if d.get("source") == "announcement" else "replies"] += 1
            elif k == "wake":
                s["wakes" if d.get("accepted") else "wakes_ignored"] += 1
            elif k == "session":
                o = d.get("outcome")
                if o == "started":
                    s["sessions_started"] += 1
                elif o == "finished":
                    s["sessions_finished"] += 1
                    if d.get("scheduled"):
                        s["checks_spoke"] += 1
                elif o == "error":
                    s["session_errors"] += 1
                elif o == "silent":
                    s["checks_silent"] += 1
                elif o == "held":
                    s["checks_held"] += 1
            elif k in ("gate", "hook") and d.get("decision") in ("approved", "auto"):
                s["approvals_yes"] += 1
            elif k in ("gate", "hook") and d.get("decision") == "denied":
                s["approvals_no"] += 1
            if k == "hook":
                s["hooks"] += 1
        return s

    @staticmethod
    def _hours(db: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
        """Event counts for each of the last 24 clock hours (oldest first)."""
        end = datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        start = end - timedelta(hours=24)
        buckets = [{"hour": (start + timedelta(hours=i)).hour, "start": (start + timedelta(hours=i)).timestamp(),
                    "total": 0, "conversations": 0} for i in range(24)]
        for r in db.execute("SELECT ts, kind FROM events WHERE ts >= ? AND kind != 'device'", (start.timestamp(),)):
            i = int((r["ts"] - start.timestamp()) // 3600)
            if 0 <= i < 24:
                buckets[i]["total"] += 1
                if r["kind"] == "heard":
                    buckets[i]["conversations"] += 1
        return buckets


# ---------------------------------------------------------------------- demo data
def _demo_png(path: Path, w: int = 390, h: int = 520) -> None:
    """A stand-in screenshot for the demo inbox: navy page with a few cream 'cards'. No image library needed."""
    import struct
    import zlib
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    navy, cream = (10, 21, 50), (246, 236, 217)
    cards = [(24, 60, 366, 150), (24, 170, 366, 260), (24, 280, 366, 370), (24, 390, 366, 480)]
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            inside = any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in cards)
            rows.extend((int(navy[i] * 0.78 + cream[i] * 0.22) for i in range(3)) if inside else navy)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def seed_demo(db_path: Path) -> None:
    """Fill a separate database with clearly synthetic activity so the page can be previewed."""
    import random
    db_path = Path(db_path).expanduser()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    db = store.connect(db_path)
    rng = random.Random(7)
    now = time.time()
    rows: list[tuple[float, str, str, dict]] = []

    def add(ago_min: float, kind: str, title: str, **detail):
        rows.append((now - ago_min * 60, kind, title, detail))

    add(60 * 23.5, "system", "Smitty started", mode="voice", transport="usb")
    convo = [("What's the weather like in Denver?", "It's sixty-eight and clear in Denver, with a high of seventy-four today."),
             ("What sessions are running?", "Two are running: the dashboard build in Smitty, and the inbox check."),
             ("Start a session in demo app and add a health check endpoint.", "I started a session called health check in demo app."),
             ("What did I miss?", "One thing overnight: Dana replied about Thursday and is waiting on a time from you."),
             ("Remind me at four to call the dentist.", "Done. I'll remind you at four this afternoon.")]
    for h in range(22, 0, -1):
        base = h * 60
        if 1 <= (24 - h) % 24 <= 7 and rng.random() < 0.8:
            continue   # a quiet night
        for _ in range(rng.choice([0, 1, 1, 2])):
            t = base - rng.uniform(0, 55)
            add(t + 0.05, "wake", "Wake word", accepted=True, heard="Hey Smitty", score=round(rng.uniform(.82, 1), 2), vad=.93)
            q, a = rng.choice(convo)
            add(t, "heard", q)
            add(t - 0.06, "said", a, source="reply", seconds=round(rng.uniform(1.6, 4.2), 1))
        for _ in range(rng.choice([0, 0, 1, 2])):
            t = base - rng.uniform(0, 55)
            if rng.random() < .55:
                add(t, "wake", "Ignored: just noise", accepted=False, reason="no speech", score=round(rng.uniform(.6, .98), 2), vad=round(rng.uniform(.05, .4), 2))
            else:
                add(t, "wake", "Ignored: not the wake word", accepted=False, reason="other speech", heard=rng.choice(["All right.", "This one? Yeah.", "It's going to be interesting."]), score=round(rng.uniform(.6, .99), 2), vad=.88)
        if h % 2 == 0:
            t = base - rng.uniform(5, 50)
            add(t, "schedule", "Fired: Inbox watch", outcome="fired")
            add(t - 0.02, "session", "Scheduled run started: Inbox watch", outcome="started", session="Inbox watch", project="smitty-tasks", scheduled=True)
            if rng.random() < .75:
                add(t - 0.7, "session", "Check ran, nothing to report: Inbox watch", outcome="silent", session="Inbox watch", project="smitty-tasks", scheduled=True, summary="[silent]", turns=3, cost_usd=0.04)
            else:
                add(t - 0.8, "session", "Session finished: Inbox watch", outcome="finished", session="Inbox watch", project="smitty-tasks", scheduled=True, summary="Dana replied about Thursday's review and is waiting on a time from you.", turns=4, cost_usd=0.06)
                add(t - 0.9, "said", "Dana replied about Thursday's review and is waiting on a time from you.", source="announcement", about="Inbox watch")
    add(318, "session", "Session started: Health check endpoint", outcome="started", session="Health check endpoint", project="demo-app", scheduled=False, prompt="Add a /health endpoint with a test.")
    add(316.5, "confirm", "Health check endpoint wants to git push: git push origin main. Allow it?", heard="Yes, go ahead.", answer="yes")
    add(316.4, "gate", "Approved: git push", session="Health check endpoint", action="git push origin main", decision="approved")
    add(314, "session", "Session finished: Health check endpoint", outcome="finished", session="Health check endpoint", project="demo-app", scheduled=False, summary="Added the health endpoint with a test and pushed it to main.", turns=9, cost_usd=0.41)
    add(190, "hook", "Your Claude Code session finished in nimbus-api", event="stop", project="nimbus-api", summary="The rate limiter now counts per API key instead of per address.")
    add(141, "hook", "Permission denied by voice: delete files", event="permission", project="storefront", action="rm -rf build", decision="denied")
    add(96, "schedule", "Scheduled: Morning briefing", outcome="created", when="daily at 08:00")
    add(41, "gate", "Denied: delete files", session="Cleanup", action="rm -rf dist", decision="denied")
    add(12, "session", "Session started: Dashboard polish", outcome="started", session="Dashboard polish", project="Smitty", scheduled=False, prompt="Tighten the dashboard spacing.")
    rows.sort()
    db.executemany("INSERT INTO events (ts, kind, title, detail) VALUES (?, ?, ?, ?)",
                   [(ts, k, t, json.dumps(d)) for ts, k, t, d in rows])
    kv = {
        "state": "idle",
        "status": {"mode": "voice", "pid": 0, "started_at": now - 23.5 * 3600, "transport": "usb", "wake_model": "hey_smitty",
                   "wake_threshold": 0.7, "volume": 0.5, "brain_model": "claude-opus-5-5", "auth": "login", "quiet_hours": "23-7",
                   "version": "0.1.0", "hooks_port": 8765},
        "sessions": [{"key": "demo1", "title": "Dashboard polish", "project": "/Users/demo/Desktop/Smitty", "status": "running",
                      "last_tool": "Edit: smitty/dashboard/index.html", "running_for_s": 720, "auto_approve": False, "turns": 1,
                      "scheduled": False, "subagents": [
                          {"type": "Explore", "description": "Find spacing tokens", "status": "done", "last_tool": "", "seconds": 21},
                          {"type": "general-purpose", "description": "Check mobile layout", "status": "running",
                           "last_tool": "Read: smitty/dashboard/index.html", "seconds": 48}]},
                     {"key": "demo2", "title": "Inbox watch", "project": "/Users/demo/Desktop/smitty-tasks", "status": "idle",
                      "last_tool": "", "running_for_s": 3400, "auto_approve": False, "turns": 6, "scheduled": True, "subagents": []}],
        "outside": [{"key": "demo3", "session_id": None, "external": True, "title": "Rate limiter per API key",
                     "project": "/Users/demo/Desktop/nimbus-api", "status": "running", "source": "Terminal", "waiting_for": "",
                     "running_for_s": 2640, "status_for_s": 95, "last_tool": "Bash: pytest tests/test_ratelimit.py -q", "scheduled": False,
                     "subagents": []},
                    {"key": "demo4", "session_id": None, "external": True, "title": "Landing page copy",
                     "project": "/Users/demo/Desktop/marketing-site", "status": "idle", "source": "Desktop app", "waiting_for": "",
                     "running_for_s": 9100, "status_for_s": 4300, "last_tool": "", "scheduled": False, "subagents": []}],
        "attention": "active", "approvals": [], "chat": {"busy": False},
        "brain": {"model": "claude-opus-5-5", "effort": "low", "turns": 31, "cost_usd": 0.2187},
        "device": {"connected": False},
        "systems": [{"name": "Microphone and speaker", "detail": "reSpeaker XVF3800 over USB", "ok": True},
                    {"name": "Wake word", "detail": "hey smitty, threshold 0.7, strict check", "ok": True},
                    {"name": "Speech to text", "detail": "Whisper large-v3-turbo (local)", "ok": True},
                    {"name": "Voice", "detail": "Kokoro bm_george (local)", "ok": True},
                    {"name": "Brain", "detail": "Claude Opus 5.5, claude.ai login", "ok": True},
                    {"name": "Scheduler", "detail": "2 schedules, quiet hours 23 to 7", "ok": True},
                    {"name": "Claude Code hooks", "detail": "listening on port 8765", "ok": True},
                    {"name": "LED ring", "detail": "USB control", "ok": True},
                    {"name": "Gmail and Calendar", "detail": "not authorized at claude.ai yet", "ok": False}],
    }
    for k, v in kv.items():
        db.execute("INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)", (k, json.dumps(v), now))
    steps = [("prompt", "Tighten the dashboard spacing.", {}),
             ("thinking", "The cards use three different paddings. I should find where spacing is defined before changing anything, then check the narrow layout still holds.", {}),
             ("agent", "Subagent started: Find spacing tokens", {"agent": "Find spacing tokens", "status": "running", "type": "Explore", "prompt": "List every padding and gap value used by the dashboard cards."}),
             ("tool", "padding|gap", {"tool": "Grep", "agent": "Find spacing tokens"}),
             ("tool_result", "index.html:111  .card { padding: 18px 20px; }\nindex.html:87   .tile { padding: 18px 18px 16px; }\nindex.html:94   .chart { padding: 20px 22px 16px; }", {"tool": "Grep", "agent": "Find spacing tokens", "error": False}),
             ("agent", "Subagent finished: Find spacing tokens", {"agent": "Find spacing tokens", "status": "done", "type": "Explore", "seconds": 21, "summary": "Three paddings in use: 18/20, 18/18/16 and 20/22/16."}),
             ("text", "Three paddings are in use. I'll settle on 18 by 20 for every card and check the phone layout in parallel.", {}),
             ("agent", "Subagent started: Check mobile layout", {"agent": "Check mobile layout", "status": "running", "type": "general-purpose", "prompt": "Confirm nothing overflows at 375px wide."}),
             ("tool", "smitty/dashboard/index.html", {"tool": "Edit"}),
             ("tool_result", "The file smitty/dashboard/index.html has been updated.", {"tool": "Edit", "error": False})]
    db.executemany("INSERT INTO steps (ts, session, kind, text, detail) VALUES (?, 'demo1', ?, ?, ?)",
                   [(now - 700 + i * 40, k, t, json.dumps(d)) for i, (k, t, d) in enumerate(steps)])
    outside_steps = [("prompt", "Rate limit by API key, not by client address.", {}),
                     ("tool", "nimbus/ratelimit.py", {"tool": "Read"}),
                     ("text", "The limiter keys on the client address only. I'll key it on the API key and add a test.", {}),
                     ("tool", "nimbus/ratelimit.py", {"tool": "Edit"}),
                     ("tool", "pytest tests/test_ratelimit.py -q", {"tool": "Bash"})]
    db.executemany("INSERT INTO steps (ts, session, kind, text, detail) VALUES (?, 'demo3', ?, ?, ?)",
                   [(now - 300 + i * 50, k, t, json.dumps(d)) for i, (k, t, d) in enumerate(outside_steps)])
    db.execute("INSERT INTO kv (key, value, updated) VALUES ('live:demo1', ?, ?)",
               (json.dumps({"kind": "text", "text": "The cards now share one padding. Checking the tiles next", "agent": None}), now))
    notes = [(190, "session_finished", "Finished: Rate limiter", "Your nimbus-api session finished. The rate limiter now counts per API key instead of per address.", None, 0, True, {"project": "nimbus-api", "own": False, "spoken": True}),
             (96, "session_finished", "Finished: Inbox watch", "Dana replied about Thursday's review and is waiting on a time from you.", "demo-inbox", 0, False, {"key": "demo2", "project": "smitty-tasks", "own": True, "spoken": False, "scheduled": True}),
             (9, "session_error", "Failed: Fare check", "The fare check could not load the airline page, so there is no price for today.", "demo-fare", 1, False, {"project": "smitty-tasks", "own": True, "spoken": False, "scheduled": True}),
             (3, "review", "Review: Dashboard polish", "Is the new spacing between the cards right on a phone?", "demo-polish", 1, False,
              {"key": "demo1", "project": "Smitty", "own": True, "image": "demo1/review.png", "review": "demo-review", "url": "http://localhost:8770"})]
    db.executemany("INSERT INTO notices (ts, kind, title, body, session, urgent, read, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(now - ago * 60, k, t, b, sid, urgent, (now - ago * 60 + 30) if read else None, json.dumps(d))
                    for ago, k, t, b, sid, urgent, read, d in notes])
    db.commit()
    db.close()
    data_dir = db_path.parent
    _demo_png(data_dir / "artifacts" / "demo1" / "review.png")
    (data_dir / "schedules.json").write_text(json.dumps([
        {"id": "a1", "title": "Inbox watch", "prompt": "", "kind": "interval", "every_minutes": 15, "at": None, "run_at": None,
         "next_run": datetime.fromtimestamp(now + 9 * 60).astimezone().isoformat(timespec="seconds"),
         "last_run": datetime.fromtimestamp(now - 6 * 60).astimezone().isoformat(timespec="seconds"), "enabled": True},
        {"id": "b2", "title": "Morning briefing", "prompt": "", "kind": "daily", "every_minutes": None, "at": "08:00", "run_at": None,
         "next_run": (datetime.now() + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0).astimezone().isoformat(timespec="seconds"),
         "last_run": None, "enabled": True}]))
    (data_dir / "missed.json").write_text("[]")


async def keep_demo_alive(db_path: Path) -> None:
    """Refresh the demo heartbeat and cycle the state so the page looks live."""
    import asyncio
    db = store.connect(db_path)
    states = ["idle", "idle", "listening", "thinking", "speaking", "idle", "idle", "idle", "needs_confirmation", "idle"]
    i = 0
    while True:
        row = db.execute("SELECT value FROM kv WHERE key = 'status'").fetchone()
        now = time.time()
        db.execute("UPDATE kv SET updated = ? WHERE key = 'status'", (now,))
        db.execute("UPDATE kv SET updated = ? WHERE key = 'live:demo1'", (now,))
        if int(now) % 4 == 0:
            i += 1
            state = states[i % len(states)]
            db.execute("UPDATE kv SET value = ?, updated = ? WHERE key = 'state'", (json.dumps(state), now))
            waiting = [{"id": "demo", "question": "Dashboard polish wants to git push: git push origin main. Allow it?", "asked_at": now,
                        "expires_at": now + 900, "spoken": True, "session": "Dashboard polish", "key": "demo1", "reason": "git push",
                        "action": "git push origin main", "project": "Smitty"}] if state == "needs_confirmation" else []
            db.execute("UPDATE kv SET value = ?, updated = ? WHERE key = 'approvals'", (json.dumps(waiting), now))
        db.commit()
        await asyncio.sleep(1)
