"""Recurring and one-off tasks: each firing starts a worker session in the tasks folder.

Schedules persist in ~/.smitty/schedules.json so they survive restarts. They only
fire while Smitty is running (this is a laptop assistant, not a cloud cron). A
missed firing (laptop asleep) runs once at the next tick rather than piling up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from smitty import store

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Schedule:
    id: str
    title: str
    prompt: str
    kind: str                       # "interval" | "daily" | "once"
    every_minutes: int | None = None
    at: str | None = None           # "HH:MM" local time for daily
    run_at: str | None = None       # ISO datetime for once
    next_run: str | None = None
    last_run: str | None = None
    last_result: str = ""
    enabled: bool = True
    created: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))

    def describe(self) -> dict[str, Any]:
        when = {"interval": f"every {self.every_minutes} minutes",
                "daily": f"daily at {self.at}",
                "once": f"once at {self.run_at}"}[self.kind]
        return {"id": self.id, "title": self.title, "when": when, "next_run": self.next_run,
                "last_run": self.last_run, "enabled": self.enabled, "prompt": self.prompt[:200]}

    def compute_next(self, after: datetime) -> datetime | None:
        if self.kind == "interval":
            return after + timedelta(minutes=self.every_minutes or 60)
        if self.kind == "daily":
            h, m = map(int, (self.at or "08:00").split(":"))
            nxt = after.replace(hour=h, minute=m, second=0, microsecond=0)
            if nxt <= after:
                nxt += timedelta(days=1)
            return nxt
        if self.kind == "once":
            t = datetime.fromisoformat(self.run_at)
            t = t if t.tzinfo else t.astimezone()
            return t if t > after else None
        return None


class Scheduler:
    def __init__(self, mgr, tasks_dir: Path, store: Path, min_interval_min: int = 15, tick_s: float = 15.0):
        self.mgr = mgr
        self.tasks_dir = Path(tasks_dir).expanduser()
        self.store = Path(store).expanduser()
        self.min_interval_min = min_interval_min
        self.tick_s = tick_s
        self.schedules: dict[str, Schedule] = {}
        self._task: asyncio.Task | None = None
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self.store.exists():
            try:
                for d in json.loads(self.store.read_text()):
                    s = Schedule(**d)
                    self.schedules[s.id] = s
            except Exception as e:
                log.warning("could not read %s: %s", self.store, e)
        log.info("scheduler: %d schedule(s) loaded from %s", len(self.schedules), self.store)

    def _save(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps([asdict(s) for s in self.schedules.values()], indent=2))

    # ------------------------------------------------------------------ API used by brain tools
    def add(self, title: str, prompt: str, every_minutes: int | None = None,
            daily_at: str | None = None, once_at: str | None = None) -> Schedule:
        if sum(x is not None for x in (every_minutes, daily_at, once_at)) != 1:
            raise ValueError("give exactly one of every_minutes, daily_at, once_at")
        if every_minutes is not None:
            if every_minutes < self.min_interval_min:
                raise ValueError(f"minimum interval is {self.min_interval_min} minutes")
            s = Schedule(id=uuid.uuid4().hex[:8], title=title, prompt=prompt, kind="interval", every_minutes=int(every_minutes))
        elif daily_at is not None:
            h, m = map(int, daily_at.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError("daily_at must be HH:MM")
            s = Schedule(id=uuid.uuid4().hex[:8], title=title, prompt=prompt, kind="daily", at=f"{h:02d}:{m:02d}")
        else:
            t = datetime.fromisoformat(once_at)
            t = t if t.tzinfo else t.astimezone()
            if t <= _now():
                raise ValueError("once_at is in the past")
            s = Schedule(id=uuid.uuid4().hex[:8], title=title, prompt=prompt, kind="once", run_at=t.isoformat(timespec="seconds"))
        nxt = s.compute_next(_now())
        s.next_run = nxt.isoformat(timespec="seconds") if nxt else None
        self.schedules[s.id] = s
        self._save()
        log.info("scheduled %r: %s (next %s)", s.title, s.describe()["when"], s.next_run)
        store.record("schedule", f"Scheduled: {s.title}", outcome="created", when=s.describe()["when"], next_run=s.next_run)
        return s

    def cancel(self, ref: str) -> Schedule | None:
        s = self.find(ref)
        if s:
            del self.schedules[s.id]
            self._save()
            store.record("schedule", f"Cancelled: {s.title}", outcome="cancelled")
        return s

    def remove(self, schedule_id: str, via: str = "dashboard") -> Schedule | None:
        """Remove by exact id (the dashboard's button), unlike cancel() which also matches titles."""
        s = self.schedules.pop(schedule_id, None)
        if s:
            self._save()
            log.info("schedule %r removed (%s)", s.title, via)
            store.record("schedule", f"Removed: {s.title}", outcome="cancelled", via=via)
        return s

    def set_enabled(self, schedule_id: str, enabled: bool, via: str = "dashboard") -> Schedule | None:
        s = self.schedules.get(schedule_id)
        if s is None:
            return None
        if enabled:
            nxt = s.compute_next(_now())
            if nxt is None:
                raise ValueError("that one-off time has already passed; remove it and make a new one")
            s.next_run = nxt.isoformat(timespec="seconds")
        s.enabled = enabled
        self._save()
        store.record("schedule", f"{'Resumed' if enabled else 'Paused'}: {s.title}", outcome="resumed" if enabled else "paused", via=via)
        return s

    async def run_now(self, schedule_id: str) -> Schedule | None:
        """Run a schedule's job once right away without changing when it is next due."""
        s = self.schedules.get(schedule_id)
        if s is None:
            return None
        if any(w.title == s.title and w.task and not w.task.done() for w in self.mgr.workers.values()):
            raise RuntimeError(f"{s.title} is already running")
        await self.mgr.start(str(self.tasks_dir), s.prompt, s.title, scheduled=True)
        s.last_run = _now().isoformat(timespec="seconds")
        self._save()
        store.record("schedule", f"Run now: {s.title}", outcome="fired", via="dashboard")
        return s

    def find(self, ref: str) -> Schedule | None:
        ref_l = ref.lower().strip()
        for s in self.schedules.values():
            if s.id.startswith(ref_l) or ref_l == s.title.lower():
                return s
        words = [w for w in ref_l.split() if len(w) > 2]
        hits = [s for s in self.schedules.values() if words and all(w in s.title.lower() for w in words)]
        return hits[0] if len(hits) == 1 else None

    def list(self) -> list[dict[str, Any]]:
        return [s.describe() for s in sorted(self.schedules.values(), key=lambda s: s.next_run or "")]

    # ------------------------------------------------------------------ running
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick_s)

    async def _tick(self) -> None:
        now = _now()
        notices = getattr(self.mgr, "notices", None)
        if notices is not None and not notices.in_quiet_hours():
            held = notices.pop_all()
            if held:
                log.info("delivering %d held announcement(s)", len(held))
                store.record("schedule", f"Delivered {len(held)} held result(s)", outcome="digest", count=len(held))
                await self.mgr.notify({"type": "missed_digest", "title": "While you were away",
                                       "project": "smitty-tasks", "count": len(held),
                                       "summary": json.dumps(held)[:1800]})
        for s in list(self.schedules.values()):
            if not s.enabled or not s.next_run:
                continue
            if datetime.fromisoformat(s.next_run) > now:
                continue
            await self._fire(s, now)

    async def _fire(self, s: Schedule, now: datetime) -> None:
        busy = [w for w in self.mgr.workers.values() if w.title == s.title and w.task and not w.task.done()]
        if busy:
            log.info("schedule %r skipped: previous run still going", s.title)
            store.record("schedule", f"Skipped (still running): {s.title}", outcome="skipped")
        else:
            log.info("firing schedule %r", s.title)
            try:
                await self.mgr.start(str(self.tasks_dir), s.prompt, s.title, scheduled=True)
                s.last_run = now.isoformat(timespec="seconds")
            except Exception as e:
                log.error("schedule %r failed to start: %s", s.title, e)
        nxt = s.compute_next(now)
        if s.kind == "once":
            s.enabled, s.next_run = False, None
        else:
            s.next_run = nxt.isoformat(timespec="seconds") if nxt else None
        self._save()
