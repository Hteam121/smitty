"""Safety rules that are enforced in code, not left to the model.

1. ``classify_tool_call`` decides whether a tool call a Claude Code session wants
   to make is destructive. Destructive calls need a spoken "yes" from the user.
2. ``parse_yes_no`` turns a transcript into True / False / None (unclear).
   Anything unclear is treated as "no" by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from smitty.brain.browser import DEFAULT_PROFILE, BrowserState, check_browser_command, is_browser_command, shell_part

# Bash commands that delete, overwrite, publish, or escalate. Checked against the
# whole command line, case-insensitive. Keep these broad: a false "are you sure?"
# is cheap, a silently deleted folder is not.
DESTRUCTIVE_BASH = [
    (r"\brm\b", "delete files"),
    (r"\brmdir\b", "remove a directory"),
    (r"\bfind\b.*\s-delete\b", "delete files"),
    (r"\bshred\b|\btruncate\b", "destroy file contents"),
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+reset\s+--hard\b", "git reset hard"),
    (r"\bgit\s+clean\b", "git clean"),
    (r"\bgit\s+checkout\s+(--\s+)?\.", "discard working changes"),
    (r"\bgit\s+restore\b", "discard working changes"),
    (r"\bgit\s+branch\s+(-D|-d|--delete)\b", "delete a branch"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "drop a stash"),
    (r"\bgit\s+rebase\b", "rewrite git history"),
    (r"\bgit\s+commit\b.*--amend", "amend a commit"),
    (r"\bgit\s+tag\s+-d\b", "delete a tag"),
    (r"\bsudo\b", "run as root"),
    (r"\bkill(all)?\b|\bpkill\b", "kill a process"),
    (r"\bchmod\b|\bchown\b", "change permissions"),
    (r"\bmkfs\b|\bdiskutil\b|\bdd\b\s", "touch a disk"),
    (r"\bnpm\s+publish\b|\btwine\s+upload\b|\buv\s+publish\b|\bcargo\s+publish\b", "publish a package"),
    (r"\b(npm|pip|uv|brew|cargo)\s+(uninstall|remove)\b", "uninstall a package"),
    (r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm)\b", "remove docker resources"),
    (r"\bdrop\s+(table|database)\b", "drop a database table"),
    (r"\blaunchctl\b|\bdefaults\s+write\b|\bosascript\b", "change system settings"),
    (r">\s*/dev/(?!null\b)", "write to a device"),
    (r"\bmv\b.*\s/dev/null", "discard a file"),
    (r"\bcurl\b.*\|\s*(ba)?sh\b|\bwget\b.*\|\s*(ba)?sh\b", "pipe a download into a shell"),
    (r"\bgh\s+(pr\s+merge|repo\s+delete|release\s+(create|delete))\b", "act on GitHub"),
    (r"\bgit\s+remote\s+(remove|rm|set-url)\b", "change git remotes"),
]
# Any tool (MCP or otherwise) whose name sounds like it changes the outside world.
DESTRUCTIVE_TOOL_NAME = re.compile(r"(delete|remove|destroy|push|publish|deploy|send|pay|purchase|drop|prune|merge)", re.I)
# Tools that reach another person (connectors: mail, calendar, Drive, chat). Asked every time; "yes to all"
# does not cover them. Matched on whole words of the tool name, so get_message and list_shared_drives pass.
CONTACT_TOOL_NAME = re.compile(
    r"(?:^|_)(send|reply|forward|respond|share|invite|post|comment|publish)(?:_|$)|(?:^|_)(create|update)_event(?:_|$)", re.I)
# Other ways to drive a browser have none of the checks in browser.py.
OTHER_BROWSER_TOOL = re.compile(r"^mcp__(?:(?!__).)*?(chrome|browser|playwright|puppeteer|computer)", re.I)


@dataclass
class Verdict:
    destructive: bool
    reason: str          # short, speakable: "delete files"
    detail: str          # what exactly: the command / path
    blocked: bool = False      # never allowed for Smitty's own sessions, not even with a spoken yes
    always_ask: bool = False   # asked every time; "yes to all" does not cover it


def _short(cmd: str, n: int = 90, cwd: str | None = None) -> str:
    """Make a command/path speakable: project-relative, whitespace-collapsed, truncated."""
    cmd = " ".join(str(cmd).split())
    if cwd:
        root = str(Path(cwd).resolve()).rstrip("/") + "/"
        cmd = cmd.replace(root, "").replace(str(Path(cwd)).rstrip("/") + "/", "")
    home = str(Path.home())
    cmd = cmd.replace(home + "/", "~/")
    return cmd if len(cmd) <= n else cmd[: n - 3] + "..."


def classify_tool_call(tool_name: str, tool_input: dict, cwd: str | None = None, vault: str | None = None,
                       browser: BrowserState | None = None, profile: str = DEFAULT_PROFILE) -> Verdict:
    """``vault`` is the user's notes folder, for sessions allowed to work in it: adding to notes and
    creating new ones is fine there, replacing a whole note or touching Obsidian's settings is not.
    ``browser`` is what the session last saw in the user's browser (see browser.py)."""
    name = tool_name.split("__")[-1] if tool_name.startswith("mcp__") else tool_name

    if name == "Bash":
        cmd = str(tool_input.get("command", ""))
        shell = cmd
        if is_browser_command(cmd):
            found = check_browser_command(cmd, browser, cwd=cwd, profile=profile)
            if found is not None:
                return Verdict(True, found.reason, found.detail, blocked=found.level == "block", always_ask=True)
            shell = shell_part(cmd)      # words inside the browser script are not shell commands
        for pattern, reason in DESTRUCTIVE_BASH:
            if re.search(pattern, shell, re.I):
                return Verdict(True, reason, _short(cmd, cwd=cwd))
        return Verdict(False, "", _short(cmd, cwd=cwd))

    if OTHER_BROWSER_TOOL.search(tool_name):
        return Verdict(True, "use a browser that has no safety checks", name, always_ask=True)
    if tool_name.startswith("mcp__") and CONTACT_TOOL_NAME.search(name):
        return Verdict(True, f"contact someone or share something ({name.replace('_', ' ')})",
                       _short(str(tool_input), cwd=cwd), always_ask=True)

    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if vault and path:
            try:
                rel = Path(path).resolve().relative_to(Path(vault).resolve())
            except ValueError:
                rel = None
            if rel is not None:
                if any(part.startswith(".") for part in rel.parts):
                    return Verdict(True, "change Obsidian's settings", str(rel))
                if name == "Write" and Path(path).exists():
                    return Verdict(True, "overwrite a note", str(rel.with_suffix("")))
                return Verdict(False, "", str(rel))
        if cwd and path:
            try:
                Path(path).resolve().relative_to(Path(cwd).resolve())
            except ValueError:
                return Verdict(True, "edit a file outside the project", _short(path, cwd=cwd))
        return Verdict(False, "", _short(path, cwd=cwd))

    if name in ("Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoWrite", "Task", "ToolSearch"):
        return Verdict(False, "", "")

    if DESTRUCTIVE_TOOL_NAME.search(name):
        return Verdict(True, f"run {name}", _short(str(tool_input), cwd=cwd))
    return Verdict(False, "", "")


YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "affirmative", "confirm", "confirmed",
    "approve", "approved", "approve it", "go ahead", "go for it", "do it", "please do", "proceed",
    "allow", "allowed", "accept", "fine", "alright", "absolutely", "correct", "right",
}
NO_WORDS = {
    "no", "nope", "nah", "negative", "cancel", "deny", "denied", "don't", "do not", "dont",
    "stop", "abort", "never mind", "nevermind", "reject", "decline", "not now", "wait", "hold on",
}


ALL_PATTERN = re.compile(r"\b(all|everything|every ?time|from now on|always|bypass|don'?t ask|stop asking)\b", re.I)


def wants_all(text: str) -> bool:
    """True if a yes-answer also asks not to be asked again ("yes, allow all edits")."""
    return bool(ALL_PATTERN.search(text or ""))


def parse_yes_no(text: str) -> bool | None:
    """True for yes, False for no, None if unclear. "No" wins over "yes" ("no, don't, yes I'm sure")."""
    t = re.sub(r"[^a-z' ]", " ", text.lower())
    t = " " + " ".join(t.split()) + " "
    if any(f" {w} " in t for w in NO_WORDS):
        return False
    if any(f" {w} " in t for w in YES_WORDS):
        return True
    return None
