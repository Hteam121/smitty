"""Screenshots for UI reviews: a page rendered by the user's own Chrome (headless), or an existing image.

    capture("http://localhost:3000", artifacts / "k1")    -> artifacts/k1/20261106-190102.png
    capture("/tmp/sim.png", artifacts / "k1")              -> copies it in

No extra install: Chrome, Chromium, Brave, Edge or Arc are tried in that order (or SMITTY_CHROME).
Images live under ~/.smitty/artifacts/<session key>/ and are referred to by the path relative to
the artifacts folder everywhere else (dashboard, hub, phone).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Arc.app/Contents/MacOS/Arc",
]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PHONE = (390, 844)          # iPhone-sized viewport by default: that is where the review is read


def browser() -> str | None:
    env = os.environ.get("SMITTY_CHROME")
    for p in ([env] if env else []) + BROWSERS:
        if p and Path(p).exists():
            return p
    return None


def is_url(target: str) -> bool:
    t = target.strip().lower()
    return t.startswith(("http://", "https://", "file://")) or t.startswith(("localhost", "127.0.0.1"))


def capture(target: str, out_dir: Path, width: int = PHONE[0], height: int = PHONE[1], full: bool = False,
            settle_ms: int = 1500, timeout_s: float = 45.0) -> Path:
    """Render a URL (or copy an image file) into out_dir. Returns the new file's path. Raises on failure."""
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    src = Path(target).expanduser()
    if not is_url(target) and src.exists():
        if src.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError(f"{src.name} is not an image")
        dest = _unique(out_dir / f"{stamp}{src.suffix.lower()}")
        shutil.copyfile(src, dest)
        return dest
    if not is_url(target):
        raise FileNotFoundError(f"{target}: not a URL and no such image file")
    url = target.strip()
    if url.startswith(("localhost", "127.0.0.1")):
        url = "http://" + url
    chrome = browser()
    if chrome is None:
        raise RuntimeError("no Chrome/Chromium found to render the page (set SMITTY_CHROME to a browser binary)")
    dest = _unique(out_dir / f"{stamp}.png")
    if full:
        height = max(height, 2400)
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
           f"--user-data-dir={out_dir.parent / '.chrome-profile'}", f"--window-size={width},{height}",
           f"--virtual-time-budget={settle_ms}", f"--screenshot={dest}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the page did not render within {timeout_s:.0f}s: {url}")
    if not dest.exists() or dest.stat().st_size == 0:
        err = (r.stderr or "").strip().splitlines()
        raise RuntimeError(f"Chrome did not produce a screenshot for {url}" + (f": {err[-1][:200]}" if err else ""))
    return dest


def relative(path: Path, artifacts_dir: Path) -> str:
    """The path as it is referred to everywhere else: relative to the artifacts folder."""
    return str(Path(path).resolve().relative_to(Path(artifacts_dir).expanduser().resolve()))


def _unique(path: Path) -> Path:
    i, p = 1, path
    while p.exists():
        p = path.with_name(f"{path.stem}-{i}{path.suffix}")
        i += 1
    return p
