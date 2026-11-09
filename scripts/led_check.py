"""Cycle the reSpeaker LED ring through every Smitty state (2 s each), then restore.

Usage: uv run scripts/led_check.py
"""

import logging
import sys
import time

from smitty.led import PATTERNS, XvfLedRing

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

ring = XvfLedRing()
print("firmware:", ring.version())
ring.save()
print("current:", {k: v for k, v in ring._saved.items()})
for state in ("idle", "listening", "thinking", "speaking", "needs_confirmation", "error", "off"):
    print(f"  -> {state:<20} {PATTERNS[state]}")
    ring.show(state)
    time.sleep(2.0)
ring.restore()
print("restored; disabled =", ring._disabled)
sys.exit(1 if ring._disabled else 0)
