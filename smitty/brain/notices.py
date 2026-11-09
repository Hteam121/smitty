"""Deciding when a scheduled job's result should be spoken.

Two rules make monitoring bearable:
  * A recurring check that found nothing replies ``[silent]`` and is not announced.
  * During quiet hours, results are held and delivered later as one digest.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SILENT = re.compile(r"\[\s*silent\s*\]", re.I)


def is_silent(text: str | None) -> bool:
    return bool(SILENT.search(text or ""))


class NoticeBoard:
    def __init__(self, store: Path, quiet_hours: str = ""):
        self.store = Path(store).expanduser()
        self.quiet = self._parse(quiet_hours)

    @staticmethod
    def _parse(spec: str) -> tuple[int, int] | None:
        """"23-7" -> (23, 7): quiet from 23:00 until 07:00. Empty/invalid -> no quiet hours."""
        m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
        if not m:
            return None
        a, b = int(m.group(1)) % 24, int(m.group(2))
        return (a, b) if a != b % 24 or b == 24 else None

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        if not self.quiet:
            return False
        h = (now or datetime.now()).hour
        a, b = self.quiet
        if b == 24:
            return h >= a
        return a <= h < b if a < b else (h >= a or h < b)

    def pending(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.store.read_text()) if self.store.exists() else []
        except Exception:
            return []

    def defer(self, event: dict[str, Any]) -> None:
        items = self.pending()
        items.append({"when": datetime.now().astimezone().isoformat(timespec="minutes"),
                      "title": event.get("title"), "project": event.get("project"),
                      "type": event.get("type"), "summary": (event.get("summary") or "")[:500]})
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(items[-50:], indent=1))
        log.info("held for later (quiet hours): %s", event.get("title"))

    def pop_all(self) -> list[dict[str, Any]]:
        items = self.pending()
        if items:
            self.store.write_text("[]")
        return items
