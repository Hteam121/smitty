"""Add Smitty's hooks to ~/.claude/settings.json (idempotent; keeps existing hooks).

Usage:  uv run scripts/install_hooks.py [--remove] [--settings PATH]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().parent / "claude-hook.sh"
MARKER = str(HOOK_SCRIPT)  # full path: only OUR script counts as ours

# event -> (matcher or None, curl timeout, hook timeout)
EVENTS = {
    "Stop": (None, 5, 10),
    "Notification": ("permission_prompt|idle_prompt", 5, 10),
    "PermissionRequest": (None, 45, 50),   # blocks while Smitty asks by voice
}


def our_entry(event: str) -> dict:
    matcher, curl_t, hook_t = EVENTS[event]
    entry = {"hooks": [{"type": "command", "command": f"'{HOOK_SCRIPT}' {event} {curl_t}", "timeout": hook_t}]}
    if matcher:
        entry["matcher"] = matcher
    return entry


def is_ours(group: dict) -> bool:
    return any(MARKER in str(h.get("command", "")) for h in group.get("hooks", []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    args = ap.parse_args()
    path = Path(args.settings)
    settings = json.loads(path.read_text()) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})

    backup = path.with_name(f"settings.json.smitty-backup-{int(time.time())}")
    if path.exists():
        shutil.copy(path, backup)

    for event in EVENTS:
        groups = [g for g in hooks.get(event, []) if not is_ours(g)]
        if not args.remove:
            groups.append(our_entry(event))
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(("removed Smitty hooks from" if args.remove else "installed Smitty hooks into"), path)
    print("backup:", backup)
    for event in EVENTS:
        print(f"  {event}: {len(hooks.get(event, []))} hook group(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
