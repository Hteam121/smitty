"""The dashboard's way to tell a running Smitty to do something.

Routes are added to the hooks server (127.0.0.1:8765) under /control/ and require the
token in ~/.smitty/control.token (written at start, readable only by the user). The
dashboard server reads that token and forwards the browser's requests; nothing else on
the machine is meant to call these.

    POST /control/chat      {text, about?}            talk to the brain in text
    POST /control/schedule  {id, action}              remove | pause | resume | run
    POST /control/session   {key, action, text?}      stop | close | send
                            {key, action: "model", model}   run on another model ("" = the default); key is a
                                                      session, "default" (new sessions) or "brain" (Smitty itself)
    POST /control/approval  {id, answer}              yes | no | all
    POST /control/notice    {id, text}                reply to a notification
    POST /control/mode      {mode}                    active | passive
    POST /control/volume    {volume}                  0..1
    POST /control/held      {}                        discard results held for quiet hours
    GET  /control/ping
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from smitty import store
from smitty.brain import models

log = logging.getLogger(__name__)

TOKEN_FILE = "control.token"
HEADER = "X-Smitty-Token"
MAX_TEXT = 4000


def write_token(data_dir: Path) -> str:
    token = secrets.token_urlsafe(32)
    path = Path(data_dir) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(path, 0o600)
    return token


def read_token(data_dir: Path) -> str | None:
    try:
        return (Path(data_dir) / TOKEN_FILE).read_text().strip() or None
    except OSError:
        return None


class Control:
    def __init__(self, brain, attention, data_dir: Path, mode: str, base_state: Callable[[], str] = lambda: "idle",
                 volume=None):
        self.brain, self.attention, self.volume = brain, attention, volume
        self.mgr, self.scheduler = brain.mgr, brain.scheduler
        self.data_dir = Path(data_dir)
        self.mode = mode
        self.base_state = base_state            # what the state falls back to after a typed turn
        self.token = write_token(self.data_dir)
        self._chatting = 0
        self._tasks: set[asyncio.Task] = set()

    def attach(self, app: web.Application) -> None:
        app.add_routes([
            web.get("/control/ping", self._guard(self.ping)),
            web.post("/control/chat", self._guard(self.chat)),
            web.post("/control/schedule", self._guard(self.schedule)),
            web.post("/control/session", self._guard(self.session)),
            web.post("/control/approval", self._guard(self.approval)),
            web.post("/control/notice", self._guard(self.notice)),
            web.post("/control/mode", self._guard(self.set_mode)),
            web.post("/control/volume", self._guard(self.set_volume)),
            web.post("/control/held", self._guard(self.held)),
        ])

    def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        try:
            (self.data_dir / TOKEN_FILE).unlink()
        except OSError:
            pass
        store.put("chat", {"busy": False})

    # ------------------------------------------------------------------ plumbing
    def _guard(self, handler):
        async def wrapped(request: web.Request) -> web.Response:
            if not secrets.compare_digest(request.headers.get(HEADER, ""), self.token):
                return web.json_response({"error": "not allowed"}, status=403)
            try:
                body = await request.json() if request.can_read_body else {}
            except Exception:
                return web.json_response({"error": "bad json"}, status=400)
            try:
                return web.json_response(await handler(body if isinstance(body, dict) else {}))
            except (ValueError, KeyError, RuntimeError) as e:
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:
                log.exception("control request failed")
                return web.json_response({"error": str(e)}, status=500)
        return wrapped

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    @staticmethod
    def _text(body: dict) -> str:
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("text required")
        return text[:MAX_TEXT]

    # ------------------------------------------------------------------ chat
    async def _chat_turn(self, shown: str, prompt: str, about: str | None = None) -> None:
        self._chatting += 1
        store.record("heard", shown, source="typed", **({"about": about} if about else {}))
        store.put("chat", {"busy": True, "since": time.time()})
        store.put("state", "thinking")
        t0 = time.monotonic()
        try:
            reply = await self.brain.ask(prompt)
        except Exception as e:
            log.exception("typed turn failed")
            reply = f"Sorry, that failed: {e}"
        finally:
            self._chatting -= 1
        store.record("said", reply, source="reply", typed=True, seconds=round(time.monotonic() - t0, 1))
        if not self._chatting:
            store.put("chat", {"busy": False})
            store.put("state", self.base_state())

    async def chat(self, body: dict) -> dict:
        text = self._text(body)
        self._spawn(self._chat_turn(text, f"[typed] {text}"))
        return {"ok": True}

    # ------------------------------------------------------------------ notifications
    async def notice(self, body: dict) -> dict:
        text = self._text(body)
        n = store.get_notice(int(body.get("id") or 0))
        if n is None:
            raise ValueError("that notification no longer exists")
        store.mark_read([n["id"]])
        subject = n["title"].split(": ", 1)[-1]
        sid, d = n.get("session"), n.get("detail") or {}
        if d.get("review"):
            # A screenshot the session is waiting on: the reply is its answer.
            r = self.attention.answer_review(str(d["review"]), text, via=str(body.get("via") or "dashboard"))
            if r is not None:
                store.record("heard", text, source="typed", about=subject)
                return {"ok": True, "sent_to": r.context.get("session") or subject, "key": r.context.get("key")}
            # Already answered or timed out: fall through and treat it as a normal reply to that session.
        if sid and d.get("own", True):
            # One of Smitty's own sessions: continue the work right where it stopped.
            live = self.mgr.workers.get(sid)
            if live and live.task and not live.task.done():
                raise RuntimeError(f"{live.name} is still working; wait for it to finish or stop it first")
            store.record("heard", text, source="typed", about=subject)
            w = await self.mgr.send(sid, text, key=d.get("key"), origin="dashboard")
            return {"ok": True, "sent_to": w.name, "key": w.key}
        self._spawn(self._chat_turn(text, f"[typed, about the notification \"{n['title']}\": {n['body'][:500]}] {text}", about=subject))
        return {"ok": True, "sent_to": "Smitty"}

    # ------------------------------------------------------------------ schedules
    async def schedule(self, body: dict) -> dict:
        sid, action = str(body.get("id") or ""), str(body.get("action") or "")
        s = self.scheduler.schedules.get(sid)
        if s is None:
            raise ValueError("no such schedule")
        if action == "remove":
            self.scheduler.remove(sid, via="dashboard")
        elif action in ("pause", "resume"):
            self.scheduler.set_enabled(sid, action == "resume", via="dashboard")
        elif action == "run":
            await self.scheduler.run_now(sid)
        else:
            raise ValueError("action must be remove, pause, resume or run")
        return {"ok": True, "title": s.title}

    async def held(self, body: dict) -> dict:
        n = len(self.mgr.notices.pop_all()) if self.mgr.notices is not None else 0
        if n:
            store.record("schedule", f"Discarded {n} held result(s)", outcome="discarded", via="dashboard")
        return {"ok": True, "discarded": n}

    # ------------------------------------------------------------------ sessions
    async def _set_model(self, target: str, model: str | None, via: str) -> dict:
        """Choose the model for one open session, for new sessions ("default") or for Smitty itself ("brain").
        The last two are remembered for the next start."""
        if target in ("default", "brain"):
            if target == "default":
                now = self.mgr.set_default_model(model, via=via)
            else:
                now = await self.brain.set_model(model, via=via)
            base = self.mgr.base_worker_model if target == "default" else self.brain.base_model
            prefs = store.get("model_prefs") or {}
            prefs.pop(target, None) if now == base else prefs.update({target: now})     # the configured one needs no note
            store.put("model_prefs", prefs)
            title = "New sessions" if target == "default" else "Smitty"
        else:
            w = self.mgr.by_key(target)
            if w is None:
                raise ValueError("that session is no longer open")
            await self.mgr.set_model(w, model, via=via)
            now, title = w.model, w.name
            store.put("sessions", self.mgr.describe_all())
        if hasattr(self.brain, "models_snapshot"):
            store.put("models", self.brain.models_snapshot())
        return {"ok": True, "title": title, "model": now}

    async def session(self, body: dict) -> dict:
        if str(body.get("action") or "") == "model":
            return await self._set_model(str(body.get("key") or ""), models.clean(body.get("model") or body.get("text")),
                                         via=str(body.get("via") or "dashboard"))
        w = self.mgr.by_key(str(body.get("key") or ""))
        if w is None:
            raise ValueError("that session is no longer open")
        action = str(body.get("action") or "")
        if action == "stop":
            if w.session_id and w.task and not w.task.done():
                await self.mgr.interrupt(w.session_id)
                store.record("session", f"Stopped from the dashboard: {w.name}", outcome="stopped", session=w.name,
                             project=w.project.name, key=w.key)
            else:
                await self.mgr.close(w)
        elif action == "close":
            await self.mgr.close(w)
        elif action == "send":
            text = self._text(body)
            if not w.session_id:
                raise RuntimeError("that session is still starting")
            if w.task and not w.task.done():
                raise RuntimeError(f"{w.name} is still working; wait for it to finish or stop it first")
            store.record("heard", text, source="typed", about=w.name)
            await self.mgr.send(w.session_id, text, origin="dashboard")
        else:
            raise ValueError("action must be stop, close, send or model")
        store.put("sessions", self.mgr.describe_all())     # so the page shows the change on its next refresh
        return {"ok": True, "title": w.name}

    # ------------------------------------------------------------------ approvals and mode
    async def approval(self, body: dict) -> dict:
        if not self.attention.resolve(str(body.get("id") or ""), str(body.get("answer") or "")):
            raise ValueError("that approval is no longer waiting")
        return {"ok": True}

    async def set_mode(self, body: dict) -> dict:
        return {"ok": True, "mode": self.attention.set_mode(str(body.get("mode") or ""), via="dashboard")}

    async def set_volume(self, body: dict) -> dict:
        if self.volume is None:
            raise RuntimeError("volume is not available")
        try:
            value = float(body.get("volume"))
        except (TypeError, ValueError):
            raise ValueError("volume must be a number from 0 to 1")
        return {"ok": True, "volume": self.volume.set(value, via="dashboard")}

    async def ping(self, body: dict) -> dict:
        return {"ok": True, "mode": self.mode, "attention": self.attention.mode, "pid": os.getpid()}
