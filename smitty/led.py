"""LED ring status on the reSpeaker XVF3800 via its USB vendor control interface.

Protocol (from Seeed's python_control/xvf_host.py): vendor control transfers to
the device, bRequest 0, wIndex = resource id, wValue = command id (| 0x80 for
reads, whose first returned byte is a status: 0 = ok, 64 = retry).
LED commands live on resource 20:
    LED_EFFECT 12 (0 off, 1 breath, 2 rainbow, 3 single colour, 4 DoA, 5 ring)
    LED_BRIGHTNESS 13, LED_GAMMIFY 14, LED_SPEED 15   (uint8; breath/rainbow)
    LED_COLOR 16 (uint32 0xRRGGBB), LED_DOA_COLOR 17 (base, doa), LED_RING_COLOR 19 (12x)
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

VID, PID = 0x2886, 0x001A
LED_RESID = 20
CMD = {"VERSION": (48, 0, 3, "u8"), "LED_EFFECT": (20, 12, 1, "u8"), "LED_BRIGHTNESS": (20, 13, 1, "u8"),
       "LED_GAMMIFY": (20, 14, 1, "u8"), "LED_SPEED": (20, 15, 1, "u8"), "LED_COLOR": (20, 16, 1, "u32"),
       "LED_DOA_COLOR": (20, 17, 2, "u32"), "LED_RING_COLOR": (20, 19, 12, "u32")}
STATUS_OK, STATUS_RETRY = 0, 64
LIBUSB_CANDIDATES = ["/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"]


@dataclass(frozen=True)
class Pattern:
    effect: int                  # LED_EFFECT value
    color: int = 0               # for breath / single
    brightness: int = 128
    speed: int = 1
    doa_colors: tuple[int, int] | None = None   # (base, doa) for effect 4


# What each pipeline state looks like. Colours are 0xRRGGBB.
PATTERNS = {
    "idle":               Pattern(effect=3, color=0x060608),                        # barely-there glow
    "listening":          Pattern(effect=4, doa_colors=(0x000818, 0x00E0FF)),        # cyan points at the speaker
    "thinking":           Pattern(effect=1, color=0x7A2BFF, brightness=160, speed=12),  # purple breathing (device default speed is 8)
    "speaking":           Pattern(effect=1, color=0x18D060, brightness=120, speed=6),   # green breathing
    "needs_confirmation": Pattern(effect=1, color=0xFF8A00, brightness=255, speed=20),  # urgent orange
    "error":              Pattern(effect=3, color=0xFF0000),                        # solid red
    "attention":          Pattern(effect=1, color=0xFF0000, brightness=255, speed=24),  # fast red pulse: an approval is waiting
    "off":                Pattern(effect=0),
}


class XvfLedRing:
    """Thread-safe driver. Transient USB errors trigger a reconnect; the ring is only
    disabled after several consecutive failures, and even then it retries every 30 s."""

    MAX_FAILURES = 3
    RETRY_AFTER_S = 30.0

    def __init__(self):
        self._dev = None
        self._lock = threading.Lock()
        self._failures = 0
        self._disabled_until = 0.0
        self._saved: dict[str, list[int]] = {}

    @property
    def _disabled(self) -> bool:
        return time.monotonic() < self._disabled_until

    # ------------------------------------------------------------------ low level
    def _open(self) -> bool:
        if self._dev is not None:
            return True
        if self._disabled:
            return False
        try:
            import usb.backend.libusb1 as libusb1
            import usb.core
            backend = None
            for path in LIBUSB_CANDIDATES:
                backend = libusb1.get_backend(find_library=lambda n, p=path: p)
                if backend:
                    break
            dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
            if dev is None:
                raise RuntimeError("reSpeaker XVF3800 not found on USB")
            self._dev = dev
            self._failures = 0
            return True
        except Exception as e:
            self._fail(f"open: {e}")
            return False

    def _fail(self, why: str) -> None:
        self._failures += 1
        self._drop_device()
        if self._failures >= self.MAX_FAILURES:
            self._disabled_until = time.monotonic() + self.RETRY_AFTER_S
            log.warning("LED ring: %s; giving up for %.0fs", why, self.RETRY_AFTER_S)
        else:
            log.info("LED ring: %s; will reconnect", why)

    def _drop_device(self) -> None:
        if self._dev is not None:
            try:
                import usb.util
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None

    def _transfer(self, fn):
        """Run a control transfer; on failure reconnect once and retry."""
        with self._lock:
            for attempt in (1, 2):
                if not self._open():
                    return None
                try:
                    result = fn(self._dev)
                    self._failures = 0
                    return result
                except Exception as e:
                    self._fail(f"usb error ({e})")
                    if self._disabled or attempt == 2:
                        raise
        return None

    def write(self, name: str, *values: int) -> None:
        import usb.util
        resid, cmdid, count, kind = CMD[name]
        if len(values) != count:
            raise ValueError(f"{name} takes {count} values")
        payload = struct.pack("<" + ("B" if kind == "u8" else "I") * count, *values)
        self._transfer(lambda dev: dev.ctrl_transfer(
            usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, cmdid, resid, payload, 1000))

    def read(self, name: str) -> list[int]:
        import usb.util
        resid, cmdid, count, kind = CMD[name]
        size = 1 if kind == "u8" else 4

        def do_read(dev):
            for _ in range(100):
                resp = dev.ctrl_transfer(usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                                         0, 0x80 | cmdid, resid, count * size + 1, 1000)
                if resp[0] == STATUS_OK:
                    return list(struct.unpack("<" + ("B" if kind == "u8" else "I") * count, bytes(resp[1:])))
                if resp[0] != STATUS_RETRY:
                    raise RuntimeError(f"{name}: status {resp[0]}")
                time.sleep(0.01)
            raise RuntimeError(f"{name}: device kept asking to retry")

        return self._transfer(do_read) or []

    # ------------------------------------------------------------------ high level
    def version(self) -> str:
        v = self.read("VERSION")
        return ".".join(map(str, v)) if v else "?"

    def apply(self, pattern: Pattern) -> None:
        if self._disabled:
            return
        try:
            if pattern.effect in (1, 3):
                self.write("LED_COLOR", pattern.color)
            if pattern.effect in (1, 2):
                self.write("LED_BRIGHTNESS", pattern.brightness)
                self.write("LED_SPEED", pattern.speed)
            if pattern.effect == 4 and pattern.doa_colors:
                self.write("LED_DOA_COLOR", *pattern.doa_colors)
            self.write("LED_EFFECT", pattern.effect)
        except Exception as e:
            log.debug("LED pattern not applied: %s", e)  # _fail() already logged the reason

    def show(self, state: str) -> None:
        self.apply(PATTERNS.get(state, PATTERNS["off"]))

    SAVED_FILE = Path("~/.smitty/led_saved.json").expanduser()

    def save(self) -> None:
        """Remember the ring's original settings so we can put them back on exit.

        Persisted to disk the first time, so an unclean exit (kill -9, closed
        terminal) that leaves the ring in one of Smitty's patterns doesn't make
        that pattern the new "original" next run.
        """
        try:
            if self.SAVED_FILE.exists():
                self._saved = json.loads(self.SAVED_FILE.read_text())
                log.info("LED ring: firmware %s, original settings from %s (effect %s)",
                         self.version(), self.SAVED_FILE.name, self._saved.get("LED_EFFECT"))
                return
            for name in ("LED_EFFECT", "LED_BRIGHTNESS", "LED_SPEED", "LED_COLOR", "LED_DOA_COLOR"):
                self._saved[name] = self.read(name)
            self.SAVED_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.SAVED_FILE.write_text(json.dumps(self._saved))
            log.info("LED ring: firmware %s, saved effect %s to %s", self.version(), self._saved["LED_EFFECT"], self.SAVED_FILE.name)
        except Exception as e:
            log.warning("LED ring: could not read current settings: %s", e)

    def restore(self) -> None:
        if not self._saved or self._disabled:
            return
        try:
            for name, vals in self._saved.items():
                if name != "LED_EFFECT" and vals:
                    self.write(name, *vals)
            self.write("LED_EFFECT", *self._saved["LED_EFFECT"])
        except Exception as e:
            log.warning("LED ring restore failed: %s", e)


class WsLedRing(XvfLedRing):
    """Same ring, reached through the XIAO: WebSocket -> I2C -> XVF3800.

    Used when the board runs the I2S firmware and is no longer a USB device. Calls are
    made from the LED worker thread and hop onto the event loop that owns the link.
    """

    def __init__(self, link, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._link, self._loop = link, loop

    def _call(self, coro):
        if not self._link.connected:
            coro.close()
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=3)

    def write(self, name: str, *values: int) -> None:
        resid, cmdid, count, kind = CMD[name]
        if len(values) != count:
            raise ValueError(f"{name} takes {count} values")
        payload = struct.pack("<" + ("B" if kind == "u8" else "I") * count, *values)
        self._call(self._link.xmos_write(resid, cmdid, payload))

    def read(self, name: str) -> list[int]:
        resid, cmdid, count, kind = CMD[name]
        size = 1 if kind == "u8" else 4
        raw = self._call(self._link.xmos_read(resid, cmdid, count * size))
        if not raw:
            return []
        return list(struct.unpack("<" + ("B" if kind == "u8" else "I") * count, raw))

    def save(self) -> None:
        # The I2S firmware's default is the DoA effect; nothing durable to save over Wi-Fi.
        self._saved = {"LED_EFFECT": [4]}

    def restore(self) -> None:
        try:
            self.write("LED_EFFECT", 4)
        except Exception as e:
            log.debug("LED restore over Wi-Fi failed: %s", e)


class LedStatus:
    """pipeline.StatusListener that drives the ring from one worker thread, never the audio loop."""

    def __init__(self, ring: XvfLedRing | None = None):
        from concurrent.futures import ThreadPoolExecutor
        self.ring = ring or XvfLedRing()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="led")
        self._stopped = False

    def start(self) -> None:
        self.ring.save()
        self.ring.show("idle")

    def stop(self) -> None:
        """Ignore further state changes, let queued writes finish, then put the ring back."""
        self._stopped = True
        self._pool.shutdown(wait=True)
        self.ring.restore()
        log.info("LED ring restored to %s", self.ring._saved.get("LED_EFFECT"))

    def reapply(self) -> None:
        """Show the current state again (e.g. after the Wi-Fi device reconnects)."""
        if getattr(self, "_last", None):
            self._show()

    def _show(self) -> None:
        if self._stopped:
            return
        name = getattr(self, "_last", "idle")
        if getattr(self, "_attention", False) and name == "idle":
            name = "attention"            # only replaces the resting glow; listening/speaking still show
        try:
            self._pool.submit(self.ring.show, name)
        except RuntimeError:
            pass  # pool already shut down

    def attention(self, on: bool) -> None:
        """Pulse red while something is waiting for the user (an approval in passive mode)."""
        if on != getattr(self, "_attention", False):
            self._attention = on
            log.info("LED attention %s", "on" if on else "off")
            self._show()

    def on_state(self, state) -> None:
        name = getattr(state, "value", str(state))
        log.info("state -> %s", name)
        self._last = name
        self._show()
