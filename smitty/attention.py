"""How Smitty gets the user's attention: out loud, or quietly through the dashboard.

Everything that used to go straight to the speaker now passes through ``Attention``:

  notify(event)    a session finished, a scheduled check found something, a digest is due
  confirm(question) a session wants to do something destructive and needs a yes or no

Two modes, switchable from the dashboard or by voice:

  active   announce results out loud AND put them in the dashboard inbox; ask approvals by voice
  passive  inbox only, nothing unsolicited is spoken (safe during calls); a pending approval
           blinks the LED ring red until it is answered

When there is no microphone at all (text-only mode) it behaves like passive.

An approval can be answered by voice or with the Allow / Deny buttons in the dashboard,
whichever comes first. If nobody answers within ``approval_wait_s`` the action is denied.

  review(question, image) a session wants the user to look at a screenshot (a UI change) and say
           whether it is right. It lands in the inbox as a "review" notice with the image; the
           reply to that notice (dashboard, phone or watch) is the answer the session gets back.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smitty import shot, store

log = logging.getLogger(__name__)

MODES = ("active", "passive")
ANSWERS = {"yes": True, "no": False, "all": "all"}
TITLES = {"session_finished": "Finished", "session_error": "Failed", "session_needs_approval": "Needs approval",
          "session_waiting": "Waiting for you", "missed_digest": "While you were away"}
NO_REVIEW_ANSWER = "The user did not answer in time. Keep your current version and say in your summary that it still needs a look."


@dataclass
class PendingReview:
    question: str
    image: str                          # relative to the artifacts folder
    context: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    asked_at: float = field(default_factory=time.time)
    notice_id: int | None = None
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


@dataclass
class Pending:
    question: str
    context: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    asked_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    spoken: bool = False                # has it been asked out loud yet?
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())

    def describe(self) -> dict[str, Any]:
        c = self.context
        return {"id": self.id, "question": self.question, "asked_at": self.asked_at, "expires_at": self.expires_at,
                "spoken": self.spoken, "session": c.get("session"), "key": c.get("key"), "reason": c.get("reason"),
                "action": c.get("action"), "project": c.get("project")}


class Attention:
    def __init__(self, approval_wait_s: float = 600.0, default_mode: str = "active", artifacts_dir: Path | None = None,
                 review_wait_s: float = 1800.0):
        self.approval_wait_s = approval_wait_s
        self.review_wait_s = review_wait_s
        self.artifacts_dir = Path(artifacts_dir).expanduser() if artifacts_dir else Path("~/.smitty/artifacts").expanduser()
        self.default_mode = default_mode if default_mode in MODES else "active"
        self.brain: Any = None       # set by the app: phrases events (brain.announce)
        self.front: Any = None       # set by the app when there is a way to talk: .announce(event) -> text, .confirm(question)
        self.leds: Any = None        # LedStatus, when a ring is attached
        self.pending: dict[str, Pending] = {}
        self.reviews: dict[str, PendingReview] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ mode
    @property
    def mode(self) -> str:
        m = store.get("attention", self.default_mode)
        return m if m in MODES else self.default_mode

    def set_mode(self, mode: str, via: str = "voice") -> str:
        mode = mode.lower().strip()
        if mode not in MODES:
            raise ValueError("mode must be active or passive")
        if mode != self.mode:
            store.put("attention", mode)
            store.record("system", f"Switched to {mode} mode", attention=mode, via=via)
            log.info("attention mode -> %s (via %s)", mode, via)
        self._leds()
        return mode

    @property
    def speaks(self) -> bool:
        """True when unsolicited speech is allowed right now."""
        return self.front is not None and self.mode == "active"

    # ------------------------------------------------------------------ notifications
    async def notify(self, event: dict[str, Any]) -> None:
        spoken = self.speaks
        text = ""
        try:
            if spoken:
                text = await self.front.announce(event) or ""
            elif self.brain is not None:
                text = await self.brain.announce(event)
                store.record("said", text, source="announcement", about=event.get("title"), spoken=False)
        except Exception:
            log.exception("could not phrase a notification")
        etype = event.get("type") or "notice"
        own = event.get("source") != "claude_code_hook"
        store.notice(etype, f"{TITLES.get(etype, 'Update')}: {event.get('title') or event.get('project') or 'Smitty'}",
                     text or (event.get("summary") or "")[:600],
                     session=event.get("session_id"), urgent=etype in ("session_error", "session_needs_approval"),
                     key=event.get("key"), project=event.get("project"), spoken=spoken, own=own,
                     scheduled=bool(event.get("scheduled")), summary=(event.get("summary") or "")[:1200])

    # ------------------------------------------------------------------ approvals
    def _publish(self) -> None:
        store.put("approvals", [p.describe() for p in self.pending.values()])
        self._leds()

    def _leds(self) -> None:
        if self.leds is not None and hasattr(self.leds, "attention"):
            # Blink only for approvals nobody is being asked about out loud right now (and open reviews).
            self.leds.attention(any(not p.spoken or not self.speaks for p in self.pending.values()) or bool(self.reviews))

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _ask_aloud(self, p: Pending) -> None:
        p.spoken = True
        self._publish()
        try:
            answer = await self.front.confirm(p.question)
        except Exception:
            log.exception("spoken confirmation failed")
            answer = None
        if answer is not None and not p.future.done():
            self._record(p, answer, "voice", recorded=True)
            p.future.set_result(answer)
        else:
            p.spoken = False                # no clear answer: it stays open in the dashboard
            if not p.future.done():
                self._publish()

    def _record(self, p: Pending, answer: Any, via: str, recorded: bool = False) -> None:
        if not recorded:                     # the voice pipeline records its own confirm event
            label = {True: "yes", False: "no", "all": "yes to all"}.get(answer, "no answer")
            store.record("confirm", p.question, answer=label, via=via)

    async def confirm(self, question: str, **context: Any) -> Any:
        """Ask for a yes/no. Returns True, False, "all" (yes, and don't ask again), or None (no answer)."""
        p = Pending(question, context)
        p.expires_at = p.asked_at + self.approval_wait_s
        self.pending[p.id] = p
        ask: asyncio.Task | None = None
        try:
            if self.speaks:
                ask = self._spawn(self._ask_aloud(p))
            else:
                log.info("approval waiting in the dashboard (%s mode): %s", self.mode if self.front else "text", question)
            self._publish()
            try:
                return await asyncio.wait_for(asyncio.shield(p.future), timeout=self.approval_wait_s)
            except asyncio.TimeoutError:
                log.warning("approval not answered within %.0fs: %s", self.approval_wait_s, question)
                store.record("confirm", question, answer="no answer", via="timeout")
                return None
        finally:
            self.pending.pop(p.id, None)
            if not p.future.done():
                p.future.cancel()
            if ask and not ask.done():
                ask.cancel()                 # stop waiting on the voice flow; the pipeline notices and moves on
            self._publish()

    def resolve(self, pending_id: str, answer: str, via: str = "dashboard") -> bool:
        """Answer a pending approval from the dashboard. answer: yes | no | all."""
        p = self.pending.get(pending_id)
        if p is None or p.future.done() or answer not in ANSWERS:
            return False
        value = ANSWERS[answer]
        self._record(p, value, via)
        p.future.set_result(value)
        return True

    # ------------------------------------------------------------------ screenshot reviews
    async def review(self, question: str, target: str, **context: Any) -> str:
        """Show the user a screenshot (``target`` = URL to render, or an image path) and wait for their words.

        Returns the reply text; a timeout returns NO_REVIEW_ANSWER so the session can carry on sensibly.
        """
        session = context.get("session") or "A session"
        folder = self.artifacts_dir / str(context.get("key") or "misc")
        path = await asyncio.to_thread(shot.capture, target, folder)       # raises if it cannot be rendered
        image = shot.relative(path, self.artifacts_dir)
        r = PendingReview(question, image, context)
        self.reviews[r.id] = r
        r.notice_id = store.notice("review", f"Review: {session}", question, session=context.get("session_id"), urgent=True,
                                   image=image, review=r.id, key=context.get("key"), project=context.get("project"),
                                   own=True, url=target if shot.is_url(target) else None)
        store.record("review", f"{session} asked for a look: {question}"[:300], image=image, session=session, key=context.get("key"))
        log.info("review waiting in the inbox: %s (%s)", question, image)
        self._leds()
        if self.speaks and hasattr(self.front, "say"):
            try:
                await self.front.say(f"{session} wants you to look at a screenshot. It's in your inbox.")
            except Exception:
                log.exception("could not announce the review")
        try:
            return await asyncio.wait_for(asyncio.shield(r.future), timeout=self.review_wait_s)
        except asyncio.TimeoutError:
            log.warning("review not answered within %.0fs: %s", self.review_wait_s, question)
            store.record("review", f"No answer for {session}'s screenshot", outcome="timeout", image=image)
            return NO_REVIEW_ANSWER
        finally:
            self.reviews.pop(r.id, None)
            if not r.future.done():
                r.future.cancel()
            self._leds()

    def answer_review(self, review_id: str, text: str, via: str = "dashboard") -> PendingReview | None:
        """Hand the user's reply to the waiting session. None when that review is no longer open."""
        r = self.reviews.get(review_id)
        if r is None or r.future.done():
            return None
        store.record("review", f"You answered {r.context.get('session') or 'the session'}: {text}"[:300], via=via, image=r.image)
        r.future.set_result(text)
        return r

    def ask_pending_aloud(self) -> int:
        """Ask every approval that has not been spoken yet (used by a voice "status check")."""
        if self.front is None:
            return 0
        waiting = [p for p in self.pending.values() if not p.spoken and not p.future.done()]
        for p in waiting:
            self._spawn(self._ask_aloud(p))
        return len(waiting)

    # ------------------------------------------------------------------ status check
    def status(self, mark_read: bool = True) -> dict[str, Any]:
        unread = store.notices(unread_only=True, limit=12)
        out = {"mode": self.mode,
               "pending_approvals": [{"session": p.context.get("session"), "wants_to": p.context.get("reason"),
                                      "action": p.context.get("action")} for p in self.pending.values()],
               "screenshots_to_review": [{"session": r.context.get("session"), "question": r.question}
                                         for r in self.reviews.values()],
               "new_notifications": [{"when": time.strftime("%H:%M", time.localtime(n["ts"])), "title": n["title"],
                                      "text": n["body"][:400]} for n in reversed(unread)]}
        if mark_read and unread:
            store.mark_read([n["id"] for n in unread])
        return out

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for p in list(self.pending.values()) + list(self.reviews.values()):
            if not p.future.done():
                p.future.cancel()
        self.pending.clear()
        self.reviews.clear()
        store.put("approvals", [])
