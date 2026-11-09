"""Claude Code sessions open on this Mac that Smitty did not start (terminal, desktop app, IDE).

Read-only. Two things Claude Code keeps under ~/.claude make this possible:

    sessions/<pid>.json                    one file per running Claude Code process: session id,
                                           name, folder, status (busy / shell / idle / waiting)
    projects/<folder>/<session-id>.jsonl   the session's transcript, appended to as it works
    projects/<folder>/<session-id>/subagents/agent-*.jsonl (+ .meta.json)   its subagents

Neither is a documented interface (seen with Claude Code 2.1.317), so every reader here treats
a missing or oddly shaped file as "nothing to show" and never raises.

``live_sessions`` returns rows shaped like Smitty's own session rows, with ``external: True`` and
a key of ``cc-<session id>``. ``read_steps`` turns a transcript into the same step records the
dashboard drawer already renders (see smitty/brain/stream.py). Step ids are byte offsets into the
transcript, so following a session is a seek, not a re-read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PREFIX = "cc-"
STATUS = {"busy": "running", "shell": "running", "waiting": "waiting", "idle": "idle"}
SOURCE = {"claude-desktop": "Desktop app", "cli": "Terminal", "claude-vscode": "VS Code", "claude-jetbrains": "JetBrains",
          "sdk-py": "Agent SDK", "sdk-ts": "Agent SDK", "sdk-cli": "Agent SDK"}
SUBAGENT_TOOLS = ("Agent", "Task")
SLOT = 64                      # step id = line offset * SLOT + position of the block within the line
TAIL_BYTES = 8 * 1024 * 1024   # first load reads at most this much from the end of a transcript
ACTIVITY_BYTES = 256 * 1024    # enough of the end to find the latest tool call
FOLLOW_BYTES = 16 * 1024 * 1024
RESULT_MAX = 1500
TEXT_MAX = 6000
SUBAGENT_QUIET_S = 300         # a subagent that wrote nothing for this long is no longer shown as running
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_parents: dict[tuple[int, Any], int | None] = {}
_paths: dict[str, Path] = {}
_activity: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_agent_calls: dict[str, dict[str, dict[str, Any]]] = {}     # session id -> tool_use_id -> subagent info


def claude_dir() -> Path:
    return Path(os.environ.get("SMITTY_CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def session_id(key: str) -> str | None:
    """The Claude Code session id behind a dashboard key, or None if the key is not one of ours."""
    sid = key[len(PREFIX):] if key.startswith(PREFIX) else ""
    return sid if _UUID.match(sid) else None


def short_input(inp: Any) -> str:
    """One readable line for a tool call (same choice of fields as Smitty's own step recorder)."""
    if not isinstance(inp, dict):
        return str(inp)[:200]
    for k in ("command", "file_path", "pattern", "url", "query", "description", "prompt", "title", "project", "session"):
        if inp.get(k):
            return str(inp[k])[:300]
    return ", ".join(f"{k}={str(v)[:60]}" for k, v in list(inp.items())[:4])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True     # exists, just not ours to signal
    return True


def _parent(pid: int, started: Any) -> int | None:
    key = (pid, started)
    if key not in _parents:
        if len(_parents) > 500:
            _parents.clear()
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
            _parents[key] = int(out.strip())
        except Exception:
            _parents[key] = None
    return _parents[key]


def transcript_path(sid: str, cwd: str | None = None, root: Path | None = None) -> Path | None:
    if not _UUID.match(sid or ""):
        return None
    projects = (root or claude_dir()) / "projects"
    cached = _paths.get(sid)
    if cached and cached.exists() and projects in cached.parents:
        return cached
    found: Path | None = None
    if cwd:
        p = projects / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl"
        found = p if p.exists() else None
    if found is None:     # the session moved folders after it started: look in every project
        try:
            found = next(iter(projects.glob(f"*/{sid}.jsonl")), None)
        except OSError:
            found = None
    if found:
        if len(_paths) > 500:
            _paths.clear()
        _paths[sid] = found
    return found


def _ts(d: dict) -> float:
    try:
        return datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def _lines(path: Path, start: int, budget: int):
    """Yield (offset, parsed line) for complete assistant/user lines from ``start``, then (end offset, None)."""
    with open(path, "rb") as f:
        f.seek(start)
        pos = start
        while pos - start < budget:
            raw = f.readline()
            if not raw.endswith(b"\n"):
                break     # end of file, or a line still being written
            here, pos = pos, pos + len(raw)
            if not raw.startswith((b'{"type":"assistant"', b'{"type":"user"')) and b'"parentUuid"' not in raw[:400]:
                continue  # bookkeeping lines (request copies, snapshots, titles) can be megabytes: skip unparsed
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("type") in ("assistant", "user") and isinstance(d.get("message"), dict):
                yield here, d
        yield pos, None


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content if isinstance(content, list) else [content]:
        parts.append((c.get("text") or f"[{c.get('type', 'content')}]") if isinstance(c, dict) else str(c))
    return "\n".join(parts)


def _steps_of(sid: str, offset: int, d: dict) -> list[dict[str, Any]]:
    """The step records for one transcript line."""
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return []
    ts, out = _ts(d), []
    calls = _agent_calls.setdefault(sid, {})
    content = d["message"].get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}] if isinstance(content, str) else []

    def add(i: int, kind: str, text: str, **detail: Any) -> None:
        out.append({"id": offset * SLOT + min(i, SLOT - 1), "ts": ts, "kind": kind, "text": text, "detail": detail})

    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if d["type"] == "assistant":
            if t == "thinking" and str(b.get("thinking") or "").strip():
                add(i, "thinking", b["thinking"].strip()[:TEXT_MAX])
            elif t == "text" and str(b.get("text") or "").strip():
                add(i, "text", b["text"].strip()[:TEXT_MAX])
            elif t == "tool_use":
                name, inp = str(b.get("name") or "tool"), b.get("input") if isinstance(b.get("input"), dict) else {}
                if name in SUBAGENT_TOOLS:
                    info = {"type": inp.get("subagent_type") or "general-purpose", "description": str(inp.get("description") or ""), "ts": ts}
                    if len(calls) > 200:
                        calls.clear()
                    calls[str(b.get("id"))] = info
                    add(i, "agent", f"Subagent started: {info['description'] or info['type']}", agent=info["description"] or info["type"],
                        status="running", type=info["type"], prompt=str(inp.get("prompt") or "")[:900])
                else:
                    add(i, "tool", short_input(inp), tool=name.split("__")[-1])
        elif t == "text":
            text = str(b.get("text") or "").strip()
            if text and not text.startswith(("<system-reminder>", "<local-command", "<command-name>", "<task-notification>")):
                add(i, "prompt", text[:TEXT_MAX])
        elif t == "tool_result":
            body, bad = _result_text(b.get("content")).strip(), bool(b.get("is_error"))
            info = calls.get(str(b.get("tool_use_id")))
            if info and "background" in body[:200].lower():
                continue          # a background subagent only reports that it was launched
            if info:
                add(i, "agent", f"Subagent {'failed' if bad else 'finished'}: {info['description'] or info['type']}",
                    agent=info["description"] or info["type"], status="failed" if bad else "done", type=info["type"],
                    summary=body[:600], seconds=max(0, int(ts - info["ts"])))
            elif body or bad:
                add(i, "tool_result", body[:RESULT_MAX] + ("\n…" if len(body) > RESULT_MAX else ""), error=bad)
    return out


def read_steps(sid: str, after: int = 0, limit: int = 400, cwd: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """Steps from a session's transcript: the latest ``limit`` when ``after`` is 0, otherwise what was
    written since the cursor (``after``) that the previous call returned."""
    out: dict[str, Any] = {"steps": [], "after": after}
    try:
        path = transcript_path(sid, cwd, root)
        if path is None:
            return out
        size = path.stat().st_size
        if after and after <= size:
            start = after
        else:   # first load (or the file was replaced): the end of the transcript
            start = max(0, size - TAIL_BYTES)
            if start:
                with open(path, "rb") as f:
                    f.seek(start)
                    start += len(f.readline())      # skip the line we landed in the middle of
        steps: list[dict[str, Any]] = []
        for offset, d in _lines(path, start, FOLLOW_BYTES):
            if d is None:
                out["after"] = offset
            else:
                steps.extend(_steps_of(sid, offset, d))
        out["steps"] = steps[-limit:]
    except Exception:
        pass
    return out


def _latest(path: Path) -> dict[str, Any]:
    """What a transcript's last stretch says about now: the latest tool call, and whether the last reply ended."""
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _activity.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    info: dict[str, Any] = {"last_tool": "", "ended": False, "mtime": st.st_mtime, "first_ts": None}
    try:
        start = max(0, st.st_size - ACTIVITY_BYTES)
        if start:
            with open(path, "rb") as f:
                f.seek(start)
                start += len(f.readline())
        for _, d in _lines(path, start, ACTIVITY_BYTES * 2):
            if d is None or d["type"] != "assistant":
                continue
            blocks = d["message"].get("content")
            tools = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"] if isinstance(blocks, list) else []
            if tools:
                info["last_tool"] = f"{str(tools[-1].get('name') or 'tool').split('__')[-1]}: {short_input(tools[-1].get('input'))}"[:160]
            info["ended"] = not tools and d["message"].get("stop_reason") == "end_turn"
    except Exception:
        pass
    if len(_activity) > 300:
        _activity.clear()
    _activity[str(path)] = (stamp, info)
    return info


def _subagents(transcript: Path, now: float) -> list[dict[str, Any]]:
    """Subagents of a session that still look busy: not finished, and written to recently."""
    out = []
    try:
        metas = sorted((transcript.with_suffix("") / "subagents").glob("agent-*.meta.json"))
    except OSError:
        return out
    for meta in metas[-40:]:
        log = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
        try:
            st = log.stat()
            if now - st.st_mtime > SUBAGENT_QUIET_S:
                continue
            m = json.loads(meta.read_text())
        except Exception:
            continue
        latest = _latest(log)
        if latest.get("ended"):
            continue
        out.append({"type": str(m.get("agentType") or "agent"), "description": str(m.get("description") or ""), "status": "running",
                    "last_tool": latest.get("last_tool", ""), "seconds": int(max(0, now - getattr(st, "st_birthtime", st.st_mtime)))})
    return out[-8:]


def live_sessions(smitty_pid: int | None = None, own_ids: set[str] | frozenset[str] = frozenset(),
                  root: Path | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """Every Claude Code session running on this Mac, minus Smitty's own; the ones needing attention or working first."""
    root, now = root or claude_dir(), now or time.time()
    rows: list[dict[str, Any]] = []
    try:
        files = list((root / "sessions").glob("*.json"))
    except OSError:
        return rows
    for f in files:
        try:
            d = json.loads(f.read_text())
            pid, sid = d.get("pid"), d.get("sessionId")
            if not isinstance(pid, int) or not isinstance(sid, str) or not _UUID.match(sid):
                continue
            if d.get("spare") or sid in own_ids or not _alive(pid):
                continue
            entry = str(d.get("entrypoint") or "")
            if smitty_pid and entry.startswith("sdk") and _parent(pid, d.get("startedAt")) == smitty_pid:
                continue          # Smitty's brain or one of its workers: already on the dashboard
            cwd = str(d.get("cwd") or "")
            status = STATUS.get(str(d.get("status") or ""), str(d.get("status") or "idle"))
            changed = (d.get("statusUpdatedAt") or d.get("updatedAt") or d.get("startedAt") or now * 1000) / 1000
            row = {"key": PREFIX + sid, "session_id": sid, "external": True, "title": str(d.get("name") or Path(cwd).name or "Claude Code session"),
                   "project": cwd, "status": status, "source": SOURCE.get(entry, "Claude Code"),
                   "waiting_for": str(d.get("waitingFor") or "") if status == "waiting" else "",
                   "running_for_s": int(max(0, now - (d.get("startedAt") or now * 1000) / 1000)),
                   "status_for_s": int(max(0, now - changed)), "last_tool": "", "subagents": [], "scheduled": False}
            path = transcript_path(sid, cwd, root)
            if path is not None:
                if status == "running":
                    row["last_tool"] = _latest(path).get("last_tool", "")
                row["subagents"] = _subagents(path, now)
            rows.append(row)
        except Exception:
            continue
    order = {"waiting": 0, "running": 1}
    rows.sort(key=lambda r: (order.get(r["status"], 2), r["status_for_s"]))
    return rows
