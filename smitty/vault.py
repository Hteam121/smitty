"""The user's Obsidian vault: a folder of Markdown notes Smitty can search, read and add to.

A vault is plain files, so there is no API here: just paths kept inside the vault root.
Nothing in this module deletes, renames or overwrites. Notes are created (never over an
existing one) or appended to. Anything bigger happens in a task session, where the
spoken-yes gate covers overwrites and deletes.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_BAD_TITLE = re.compile(r'[\\/:*?"<>|#^\[\]]')   # characters Obsidian (or the filesystem) rejects in a note name


class VaultError(Exception):
    """Something the user should hear: no such note, a name that matches several, a note that exists."""


class Vault:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    # ------------------------------------------------------------------ paths
    def _notes(self) -> list[Path]:
        """Every note, skipping hidden folders (.obsidian settings, .trash)."""
        out = []
        for p in self.root.rglob("*.md"):
            if not any(part.startswith(".") for part in p.relative_to(self.root).parts):
                out.append(p)
        return out

    def name(self, path: Path) -> str:
        """How a note is referred to: its path inside the vault, without the extension."""
        return str(path.relative_to(self.root).with_suffix(""))

    def _inside(self, rel: str) -> Path:
        rel = rel.strip().strip("/")
        if not rel.lower().endswith(".md"):
            rel += ".md"
        path = (self.root / rel).resolve()
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            raise VaultError("that path is outside the vault") from None
        if any(part.startswith(".") for part in parts):
            raise VaultError("that folder holds Obsidian's own settings, not notes")
        return path

    def resolve(self, ref: str) -> Path:
        """Find one note from a spoken name: exact path, exact title, all words, then a near miss."""
        ref_l = ref.strip().lower().removesuffix(".md").strip("/")
        if not ref_l:
            raise VaultError("which note?")
        notes = self._notes()
        by_name = {self.name(p).lower(): p for p in notes}
        if ref_l in by_name:
            return by_name[ref_l]
        hits = [p for p in notes if p.stem.lower() == ref_l]
        if not hits:
            words = re.findall(r"\w+", ref_l)
            hits = [p for p in notes if words and all(w in self.name(p).lower() for w in words)]
        if not hits:
            stems = {p.stem.lower(): p for p in notes}
            hits = [stems[s] for s in difflib.get_close_matches(ref_l, list(stems), n=3, cutoff=0.75)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise VaultError(f"no note matches {ref!r}")
        raise VaultError(f"{ref!r} matches several notes: " + ", ".join(self.name(p) for p in hits[:6]))

    # ------------------------------------------------------------------ reading
    def recent(self, limit: int = 8) -> list[dict]:
        notes = sorted(self._notes(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [{"note": self.name(p), "modified": _when(p)} for p in notes]

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Notes whose title or text contains the query's words, best match first."""
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
        if not terms:
            return self.recent(limit)
        scored = []
        for p in self._notes():
            try:
                body = p.read_text(errors="replace")
            except OSError:
                continue
            title, low = self.name(p).lower(), body.lower()
            found = [t for t in terms if t in title or t in low]
            if not found:
                continue
            score = (len(found) == len(terms)) * 100 + sum(10 * (t in title) + min(low.count(t), 5) for t in found)
            scored.append((score, p.stat().st_mtime, p, _snippet(body, terms)))
        scored.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return [{"note": self.name(p), "modified": _when(p), "snippet": snip} for _, _, p, snip in scored[:limit]]

    def read(self, ref: str, max_chars: int = 6000) -> dict:
        path = self.resolve(ref)
        body = path.read_text(errors="replace")
        return {"note": self.name(path), "modified": _when(path), "text": body[:max_chars],
                "truncated": len(body) > max_chars}

    # ------------------------------------------------------------------ writing (add only)
    def daily_path(self, day: datetime | None = None) -> Path:
        """Today's daily note, where Obsidian's own Daily notes button would put it."""
        day = day or datetime.now()
        folder, fmt = "", "%Y-%m-%d"
        try:
            cfg = json.loads((self.root / ".obsidian" / "daily-notes.json").read_text())
            folder = str(cfg.get("folder") or "")
            custom = str(cfg.get("format") or "").replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")
            if custom and not re.search(r"[A-Za-z]", custom.replace("%Y", "").replace("%m", "").replace("%d", "")):
                fmt = custom
        except (OSError, ValueError):
            pass
        return self._inside(f"{folder}/{day.strftime(fmt)}")

    def add_to_daily(self, text: str) -> str:
        """Append a timestamped line to today's daily note, creating the note if needed."""
        text = " ".join(text.split())
        if not text:
            raise VaultError("there is nothing to add")
        path = self.daily_path()
        self._append(path, f"- {datetime.now().strftime('%H:%M')} {text}")
        return self.name(path)

    def append(self, ref: str, text: str) -> str:
        if not text.strip():
            raise VaultError("there is nothing to add")
        path = self.resolve(ref)
        self._append(path, text.strip())
        return self.name(path)

    def create(self, title: str, content: str = "", folder: str = "") -> str:
        title = " ".join(_BAD_TITLE.sub(" ", title).split()).strip(". ")
        if not title:
            raise VaultError("the note needs a title")
        path = self._inside(f"{folder}/{title}")
        if path.exists():
            raise VaultError(f"a note called {self.name(path)!r} already exists; append to it instead")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as f:           # "x": never replaces a note, even in a race
            f.write(content.strip() + "\n" if content.strip() else "")
        return self.name(path)

    @staticmethod
    def _append(path: Path, block: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(errors="replace") if path.exists() else ""
        lead = "" if not existing or existing.endswith("\n") else "\n"
        with path.open("a") as f:
            f.write(f"{lead}{block}\n")


def _when(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _snippet(body: str, terms: list[str], width: int = 180) -> str:
    for line in body.splitlines():
        if any(t in line.lower() for t in terms):
            line = " ".join(line.split())
            return line if len(line) <= width else line[: width - 3] + "..."
    return ""


def open_vault(path: Path | None) -> Vault | None:
    """The configured vault, or None (with a log line) when there isn't one to use."""
    if not path:
        return None
    if not Path(path).is_dir():
        log.warning("vault folder %s does not exist; note tools are off (set SMITTY_VAULT_DIR)", path)
        return None
    return Vault(path)
