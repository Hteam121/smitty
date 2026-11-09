"""Which Claude model each session runs on, and what each turn used.

Model ids are passed to Claude Code as they are. The picker's choices are the models the installed
Claude Code says this account can use (``choices``): it refuses to switch a running session to a model
it does not list. ``UsageMeter`` turns the
running per-model totals Claude Code reports at the end of every turn into what that turn added, and
writes it to the store's ``usage`` table.
"""

from __future__ import annotations

import re
from typing import Any

from smitty import store

FALLBACK = ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"]   # offered when Claude Code's list cannot be read
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{1,79}$")
_FIELDS = {"inputTokens": "input", "outputTokens": "output", "cacheReadInputTokens": "cache_read",
           "cacheCreationInputTokens": "cache_write", "costUSD": "cost_usd"}


def clean(model: Any) -> str | None:
    """A model id from outside (the dashboard, the phone): None for "the default", else a checked id."""
    model = str(model or "").strip()
    if not model or model == "default":
        return None
    if not _ID.match(model):
        raise ValueError("that is not a model id")
    return model


def label(model: str | None) -> str:
    """claude-opus-5-5 -> Opus 5.5 (a trailing date stamp is dropped)."""
    if not model:
        return "the default model"
    parts = [p for p in model.removeprefix("claude-").split("-") if not (p.isdigit() and len(p) == 8)]
    words = " ".join(p.capitalize() for p in parts if not p.isdigit())
    return f"{words} {'.'.join(p for p in parts if p.isdigit())}".strip() or model


def choices(server_info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The models to offer, as Claude Code lists them for this account. Its short names (opus, sonnet,
    haiku) point at the current models; the ones it lists only by full id are earlier ones."""
    out: dict[str, bool] = {}
    for m in (server_info or {}).get("models") or []:
        if not isinstance(m, dict):
            continue
        value, mid = str(m.get("value") or ""), str(m.get("resolvedModel") or m.get("value") or "")
        if mid.startswith("claude-") and _ID.match(mid) and mid not in out:
            out[mid] = value == mid                 # listed by its full id only: an earlier model
    if not out:
        out = {m: False for m in FALLBACK}
    return [{"id": k, "label": label(k), "older": older} for k, older in out.items()]


class UsageMeter:
    """Records what each turn of one Claude Code process used, per model.

    A result's ``model_usage`` is the total since that process started (subagents included, each
    under its own model), so every turn is recorded as the difference from the turn before.
    """

    def __init__(self, session: str):
        self.session = session
        self._seen: dict[str, dict[str, float]] = {}

    def feed(self, model_usage: dict[str, Any] | None, source: str, title: str = "") -> dict[str, float]:
        """Record one finished turn. Returns {model: cost of this turn}. Never raises."""
        spent: dict[str, float] = {}
        try:
            for model, total in (model_usage or {}).items():
                if not isinstance(total, dict):
                    continue
                now = {col: float(total.get(key) or 0) for key, col in _FIELDS.items()}
                before = self._seen.get(model, {})
                if any(now[c] < before.get(c, 0) for c in now):
                    before = {}                       # the totals went backwards: a new process, start again
                delta = {c: now[c] - before.get(c, 0) for c in now}
                self._seen[model] = now
                if any(v > 0 for v in delta.values()):
                    store.usage(self.session, source, title, model, **delta)
                    spent[model] = round(delta["cost_usd"], 4)
        except Exception:
            pass
        return spent
