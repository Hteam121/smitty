"""Turns a session's message stream into a step-by-step record the dashboard can show.

Every Claude Code session Smitty runs (the brain and each worker) yields a stream of SDK
messages. ``StepRecorder.feed`` writes the interesting ones to the store's ``steps`` table:

    prompt       what the session was asked
    thinking     the model's reasoning, when the model shares it
    text         what the model wrote
    tool         a tool call (name + a short description of the input)
    tool_result  what came back (trimmed)
    agent        a subagent starting or finishing
    result       the end of a turn (turns, seconds, cost)
    error        a failure

With ``include_partial_messages`` on, text and reasoning also arrive as small deltas; those are
kept in a ``live:<key>`` snapshot (rewritten a few times a second) so the page can show words as
they are produced, then replaced by the finished step.
"""

from __future__ import annotations

import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from smitty import store

SUBAGENT_TOOLS = ("Agent", "Task")
LIVE_EVERY_S = 0.25
RESULT_MAX = 1500


def short_input(name: str, inp: dict) -> str:
    """One readable line for a tool call."""
    if not isinstance(inp, dict):
        return str(inp)[:200]
    for k in ("command", "file_path", "pattern", "url", "query", "description", "prompt", "title", "project", "session"):
        if inp.get(k):
            return str(inp[k])[:300]
    return ", ".join(f"{k}={str(v)[:60]}" for k, v in list(inp.items())[:4])


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, dict):
            parts.append(c.get("text") or f"[{c.get('type', 'content')}]")
        else:
            parts.append(str(c))
    return "\n".join(parts)


class StepRecorder:
    def __init__(self, key: str):
        self.key = key
        self.agents: dict[str, dict[str, Any]] = {}     # tool_use_id -> subagent info
        self._tools: dict[str, str] = {}                # tool_use_id -> tool name
        self._live_kind = ""
        self._live_text = ""
        self._live_agent: str | None = None
        self._live_at = 0.0

    # ------------------------------------------------------------------ subagents
    def subagents(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for a in list(self.agents.values())[-8:]:
            out.append({"type": a["type"], "description": a["description"], "status": a["status"],
                        "last_tool": a.get("last_tool", ""),
                        "seconds": int((a.get("ended") or now) - a["started"])})
        return out

    def _agent_name(self, parent: str | None) -> str | None:
        a = self.agents.get(parent or "")
        return (a["description"] or a["type"]) if a else None

    def _finish_agent(self, tool_use_id: str | None, status: str, summary: str = "") -> None:
        a = self.agents.get(tool_use_id or "")
        if not a or a["status"] != "running":
            return
        a["status"], a["ended"] = status, time.time()
        store.step(self.key, "agent", f"Subagent {'finished' if status == 'done' else status}: {a['description'] or a['type']}",
                   agent=a["description"] or a["type"], status=status, type=a["type"], summary=summary[:600],
                   seconds=int(a["ended"] - a["started"]))

    # ------------------------------------------------------------------ live text
    def _live(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._live_at >= LIVE_EVERY_S:
            self._live_at = now
            store.put(f"live:{self.key}", {"kind": self._live_kind, "text": self._live_text[-4000:], "agent": self._live_agent})

    def clear_live(self) -> None:
        if self._live_kind or self._live_text:
            self._live_kind, self._live_text, self._live_agent = "", "", None
            self._live(force=True)

    def _stream(self, msg: StreamEvent) -> None:
        ev = msg.event or {}
        t = ev.get("type")
        if t == "content_block_start":
            block = ev.get("content_block") or {}
            kind = {"text": "text", "thinking": "thinking", "tool_use": "tool"}.get(block.get("type"), "")
            self._live_kind, self._live_agent = kind, self._agent_name(msg.parent_tool_use_id)
            self._live_text = f"{block.get('name', 'tool')} " if kind == "tool" else ""
            if kind:
                self._live(force=True)
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            piece = d.get("text") or d.get("thinking") or ""
            if piece and self._live_kind in ("text", "thinking"):
                self._live_text += piece
                self._live()

    # ------------------------------------------------------------------ the record
    def prompt(self, text: str, **detail: Any) -> None:
        store.step(self.key, "prompt", text, **detail)

    def error(self, text: str) -> None:
        self.clear_live()
        store.step(self.key, "error", text)

    def feed(self, msg: Any) -> None:
        """Record one SDK message. Never raises: recording must not break a session."""
        try:
            self._feed(msg)
        except Exception:
            pass

    def _feed(self, msg: Any) -> None:
        if isinstance(msg, StreamEvent):
            self._stream(msg)
            return
        if isinstance(msg, AssistantMessage):
            agent = self._agent_name(msg.parent_tool_use_id)
            extra = {"agent": agent} if agent else {}
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    if block.thinking.strip():
                        store.step(self.key, "thinking", block.thinking.strip(), **extra)
                elif isinstance(block, TextBlock):
                    if block.text.strip():
                        store.step(self.key, "text", block.text.strip(), **extra)
                elif isinstance(block, ToolUseBlock):
                    self._tools[block.id] = block.name
                    inp = block.input if isinstance(block.input, dict) else {}
                    if block.name in SUBAGENT_TOOLS:
                        info = {"type": inp.get("subagent_type") or "general-purpose",
                                "description": inp.get("description") or "", "status": "running", "started": time.time()}
                        self.agents[block.id] = info
                        store.step(self.key, "agent", f"Subagent started: {info['description'] or info['type']}",
                                   agent=info["description"] or info["type"], status="running", type=info["type"],
                                   prompt=str(inp.get("prompt") or "")[:900], **({"parent": agent} if agent else {}))
                    else:
                        if agent and msg.parent_tool_use_id in self.agents:
                            self.agents[msg.parent_tool_use_id]["last_tool"] = f"{block.name}: {short_input(block.name, inp)}"[:120]
                        store.step(self.key, "tool", short_input(block.name, inp), tool=block.name.split("__")[-1], **extra)
            self.clear_live()
            return
        if isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                agent = self._agent_name(msg.parent_tool_use_id)
                for block in msg.content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    name = self._tools.pop(block.tool_use_id, "")
                    body = _result_text(block.content).strip()
                    if block.tool_use_id in self.agents:
                        # A foreground subagent's result is its final answer; a background one only says "launched".
                        if "background" in body[:200].lower():
                            self.agents[block.tool_use_id]["background"] = True
                        else:
                            self._finish_agent(block.tool_use_id, "failed" if block.is_error else "done", body)
                        continue
                    if body or block.is_error:
                        cut = len(body) > RESULT_MAX
                        store.step(self.key, "tool_result", body[:RESULT_MAX] + ("\n…" if cut else ""),
                                   tool=name.split("__")[-1], error=bool(block.is_error),
                                   **({"agent": agent} if agent else {}))
            return
        if isinstance(msg, TaskStartedMessage):
            # Background shell commands are "tasks" too; only real agents belong in the subagent list.
            if msg.tool_use_id and msg.tool_use_id not in self.agents and "bash" not in (msg.task_type or "").lower():
                self.agents[msg.tool_use_id] = {"type": msg.task_type or "task", "description": msg.description or "",
                                                "status": "running", "started": time.time()}
            return
        if isinstance(msg, TaskProgressMessage):
            a = self.agents.get(msg.tool_use_id or "")
            if a and msg.last_tool_name:
                a["last_tool"] = msg.last_tool_name
            return
        if isinstance(msg, TaskNotificationMessage):
            self._finish_agent(msg.tool_use_id, "done" if msg.status == "completed" else msg.status, msg.summary or "")
            return
        if isinstance(msg, ResultMessage):
            self.clear_live()
            for tid, a in list(self.agents.items()):
                if not a.get("background"):
                    self._finish_agent(tid, "done")
            store.step(self.key, "result", "Failed" if msg.is_error else "Finished", error=bool(msg.is_error),
                       turns=msg.num_turns, seconds=round((msg.duration_ms or 0) / 1000, 1),
                       cost_usd=round(msg.total_cost_usd or 0, 4))
