"""Local HTTP endpoint (default 127.0.0.1:8765) that Claude Code hooks POST to.

The user's own Claude Code sessions (terminal, desktop app) have hooks in
~/.claude/settings.json that pipe the hook's stdin JSON to this server via
scripts/claude-hook.sh. Smitty then:

  Stop               -> announces "session X in project Y finished: ..."
  PermissionRequest  -> asks the user by voice and returns an allow/deny
                        decision; no clear answer = no decision, so Claude
                        Code shows its normal permission dialog as a fallback
  Notification       -> announces permission_prompt (if not already handled)
                        and, optionally, idle_prompt

Sessions that Smitty itself started (workers) are skipped here: the
SessionManager already announces and gates those in-process.

Also: GET /status, POST /say {"text": ...} for scripts and debugging, and
POST /review {"target": url-or-image, "question", "session"?, "key"?, "project"?}: put a
screenshot in the user's inbox and wait (long poll) for their reply - `smitty review` uses it, so
any Claude Code session can ask the user to look at a UI change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from smitty import store
from smitty.brain.safety import classify_tool_call

log = logging.getLogger(__name__)

Confirm = Callable[[str], Awaitable[bool | None]]
Notify = Callable[[dict[str, Any]], Awaitable[None]]
Say = Callable[[str], Awaitable[None]]


def _session_title(session_id: str, cwd: str | None) -> str:
    """A short, speakable name: the session's title or summary, else 'the <project> session'."""
    project = Path(cwd).name if cwd else None
    fallback = f"the {project} session" if project else "a Claude Code session"
    try:
        from claude_agent_sdk import get_session_info
        info = get_session_info(session_id, directory=cwd) if cwd else get_session_info(session_id)
        if info:
            title = info.custom_title or info.summary or ""
            if 0 < len(title) <= 60:
                return title
    except Exception as e:
        log.debug("get_session_info failed: %s", e)
    return fallback


class HooksServer:
    def __init__(self, host: str, port: int, confirm: Confirm, notify: Notify, say: Say,
                 is_own_session: Callable[[str], bool] = lambda sid: False,
                 status: Callable[[], dict[str, Any]] = lambda: {},
                 announce_idle: bool = False, permission_timeout_s: float = 42.0,
                 review: Callable[..., Awaitable[str]] | None = None):
        self.host, self.port = host, port
        self.confirm, self.notify, self.say = confirm, notify, say
        self.review = review
        self.is_own_session = is_own_session
        self.status_fn = status
        self.announce_idle = announce_idle
        self.permission_timeout_s = permission_timeout_s
        self._recent_permission: dict[str, float] = {}   # session_id -> when we last asked
        self._runner: web.AppRunner | None = None
        self.app = web.Application()
        self.app.add_routes([
            web.post("/hook/{event}", self.handle_hook),
            web.post("/hook", self.handle_hook),
            web.get("/status", self.handle_status),
            web.post("/say", self.handle_say),
            web.post("/review", self.handle_review),
        ])

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("hooks endpoint listening on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ handlers
    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **self.status_fn()})

    async def handle_say(self, request: web.Request) -> web.Response:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        asyncio.create_task(self.say(text))
        return web.json_response({"ok": True})

    async def handle_review(self, request: web.Request) -> web.Response:
        if self.review is None:
            return web.json_response({"error": "reviews need the Claude brain running"}, status=503)
        body = await request.json()
        target, question = str(body.get("target") or "").strip(), str(body.get("question") or "").strip()
        if not target or not question:
            return web.json_response({"error": "target (url or image path) and question required"}, status=400)
        ctx = {k: body[k] for k in ("session", "key", "project") if body.get(k)}
        try:
            answer = await self.review(question, target, **ctx)
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "answer": answer})

    async def handle_hook(self, request: web.Request) -> web.Response:
        raw = await request.text()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return web.json_response({"error": "bad json"}, status=400)
        event = (request.match_info.get("event") or data.get("hook_event_name") or "").replace("-", "").replace("_", "").lower()
        sid = str(data.get("session_id", ""))
        cwd = data.get("cwd")
        project = Path(cwd).name if cwd else "unknown project"
        log.info("hook %s from %s (%s)", event, project, sid[:8])

        if sid and self.is_own_session(sid):
            log.debug("ignoring hook for Smitty's own worker %s", sid[:8])
            return web.json_response({})

        if event == "stop":
            if data.get("stop_hook_active"):
                return web.json_response({})
            summary = (data.get("last_assistant_message") or "").strip()
            store.record("hook", f"Your Claude Code session finished in {project}", event="stop",
                         project=project, summary=summary[-400:])
            asyncio.create_task(self.notify({
                "type": "session_finished", "source": "claude_code_hook", "session_id": sid,
                "title": _session_title(sid, cwd), "project": project, "summary": summary[-600:],
            }))
            return web.json_response({})

        if event == "permissionrequest":
            return await self._permission_request(data, sid, project, cwd)

        if event == "notification":
            ntype = data.get("notification_type", "")
            msg = (data.get("message") or "").strip()
            if ntype == "permission_prompt":
                if time.monotonic() - self._recent_permission.get(sid, 0) < 90:
                    return web.json_response({})  # we already asked by voice
                asyncio.create_task(self.notify({
                    "type": "session_needs_approval", "source": "claude_code_hook", "session_id": sid,
                    "title": _session_title(sid, cwd), "project": project, "summary": msg,
                    "note": "Smitty cannot answer this one by voice; the user must click in Claude Code.",
                }))
            elif ntype == "idle_prompt" and self.announce_idle:
                asyncio.create_task(self.notify({
                    "type": "session_waiting", "source": "claude_code_hook", "session_id": sid,
                    "title": _session_title(sid, cwd), "project": project, "summary": msg,
                }))
            return web.json_response({})

        log.debug("unhandled hook event %r", event)
        return web.json_response({})

    async def _permission_request(self, data: dict, sid: str, project: str, cwd: str | None) -> web.Response:
        tool = data.get("tool_name", "a tool")
        tool_input = data.get("tool_input") or {}
        verdict = classify_tool_call(tool, tool_input, cwd)
        what = verdict.reason if verdict.destructive else f"use {tool.split('__')[-1]}"
        detail = verdict.detail or ""
        title = _session_title(sid, cwd)
        where = "" if title.endswith(f"{project} session") else f" in {project}"
        question = f"{title}{where} wants to {what}{': ' + detail if detail else ''}. Allow it?"
        self._recent_permission[sid] = time.monotonic()
        try:
            answer = await asyncio.wait_for(
                self.confirm(question, session=title, reason=what, action=detail, project=project, source="claude_code_hook"),
                timeout=self.permission_timeout_s)
        except asyncio.TimeoutError:
            answer = None
        if answer is None:
            log.info("no voice decision for %s; leaving it to the Claude Code dialog", sid[:8])
            store.record("hook", f"Permission left to the dialog: {what}", event="permission", project=project,
                         action=detail, decision="undecided")
            return web.json_response({})
        decision = {"behavior": "allow"} if answer else {
            "behavior": "deny", "message": "The user declined this.", "interrupt": False}
        log.info("voice decision for %s: %s (%s)", sid[:8], decision["behavior"], detail)
        store.record("hook", f"Permission {'allowed' if answer else 'denied'}: {what}", event="permission",
                     project=project, action=detail, decision="approved" if answer else "denied")
        return web.json_response({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})
