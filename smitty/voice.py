"""Voice that comes and goes with the microphone.

Smitty's brain, scheduler, dashboard and chat run the whole time. ``VoiceLink`` adds the
voice side (wake word, speech-to-text, speech) whenever the reSpeaker is plugged in and
takes it away again when it is unplugged, without restarting anything, so the conversation
carries on across the change.

    detached   text only: chat in the dashboard, everything goes to the inbox
    attached   voice + text: the same conversation, spoken or typed

The speech models are loaded the first time a microphone appears and kept after that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from smitty import store

log = logging.getLogger(__name__)

WAITING = "Waiting for the reSpeaker: plug it in for voice"
NO_AUDIO = "The reSpeaker is plugged in but offers no microphone. It may have the Wi-Fi firmware: scripts/flash_xvf3800.sh usb"


def usb_board_seen() -> bool | None:
    """Is the XVF3800 on the USB bus at all? None when that cannot be checked."""
    try:
        import usb.backend.libusb1 as libusb1
        import usb.core
        from smitty.led import LIBUSB_CANDIDATES, PID, VID
        backend = None
        for path in LIBUSB_CANDIDATES:
            backend = libusb1.get_backend(find_library=lambda n, p=path: p)
            if backend:
                break
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is not None:
            import usb.util
            usb.util.dispose_resources(dev)
        return dev is not None
    except Exception as e:
        log.debug("USB check unavailable: %s", e)
        return None


def usb_microphone() -> tuple[bool, str]:
    """(ready, note) for the wired reSpeaker. Only call while no audio stream is open."""
    from smitty.audio.usb import find_device, refresh_devices
    seen = usb_board_seen()
    if seen is False:
        return False, WAITING          # cheap check first: nothing on the bus, no need to rescan audio
    refresh_devices()
    if find_device() is not None:
        return True, ""
    return False, NO_AUDIO if seen else WAITING


class VoiceLink:
    def __init__(self, settings, brain, attention,
                 make_audio: Callable[[], tuple[Any, Any]],
                 microphone: Callable[[], tuple[bool, str]],
                 make_pipeline: Callable[[Any, Any, Any], Any],
                 make_leds: Callable[[Any], Any] | None = None,
                 poll_s: float = 3.0, silence_s: float = 4.0, watch_frames: bool = True,
                 greeting: str = ""):
        self.settings, self.brain, self.attention = settings, brain, attention
        self.make_audio, self.microphone = make_audio, microphone
        self.make_pipeline, self.make_leds = make_pipeline, make_leds
        self.poll_s, self.silence_s, self.watch_frames = poll_s, silence_s, watch_frames
        self.greeting = greeting
        self.pipeline: Any = None            # built on first attach, then reused
        self.leds: Any = None
        self.attached = False
        self.note = WAITING                  # why voice is off, in plain words (shown in the dashboard)
        self.attaches = 0

    # ------------------------------------------------------------------ what others ask
    @property
    def mode(self) -> str:
        return "voice" if self.attached else "text"

    @property
    def state(self) -> str:
        return self.pipeline.state.value if self.attached and self.pipeline is not None else "idle"

    def apply_volume(self, value: float) -> None:
        """Change the live speaker's level; chime once at the new level if Smitty is not mid-sentence."""
        if self.pipeline is None:
            return
        self.pipeline.sink.volume = value
        if self.attached and self.pipeline.state.value == "idle" and value > 0:
            from smitty.audio.base import SAMPLE_RATE
            from smitty.pipeline import _chime
            asyncio.create_task(self.pipeline.sink.play(_chime(), SAMPLE_RATE))

    async def say(self, text: str) -> None:
        if self.attached:
            await self.pipeline.say(text)
        else:
            store.notice("message", "Message", text)

    # ------------------------------------------------------------------ the loop
    async def run(self) -> None:
        """Runs until cancelled: wait for a microphone, serve voice until it goes, repeat."""
        last_note = None
        while True:
            try:
                ready, note = await asyncio.to_thread(self.microphone)
            except Exception as e:
                ready, note = False, f"Could not check for the microphone: {e}"
            if not ready:
                self.note = note
                if note != last_note:
                    log.info("voice off: %s", note)
                    last_note = note
                await asyncio.sleep(self.poll_s)
                continue
            last_note = None
            try:
                await self._serve()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("voice stopped unexpectedly")
                self.note = f"Voice stopped: {e}"
            await asyncio.sleep(self.poll_s)

    async def _serve(self) -> None:
        source, sink = self.make_audio()
        if self.pipeline is None:
            log.info("microphone found: loading the speech models (first time only)...")
            self.note = "Microphone found: loading the speech models"
            if self.make_leds is not None:
                self.leds = self.make_leds(source)
            self.pipeline = await asyncio.to_thread(self.make_pipeline, source, sink, self.leds)
            if hasattr(self.pipeline.stt, "warm_up"):
                await asyncio.to_thread(self.pipeline.stt.warm_up)
        else:
            self.pipeline.source, self.pipeline.sink = source, sink
        p = self.pipeline
        started: list[Any] = []
        run: asyncio.Task | None = None
        try:
            for dev in (source, sink):
                await dev.start()
                started.append(dev)
            self._attach()
            run = asyncio.create_task(p.run(), name="voice-pipeline")
            if self.greeting and self.attention.mode == "active":
                asyncio.create_task(p.say(self.greeting))
            await self._watch(run, source)
        finally:
            if run is not None and not run.done():
                run.cancel()
                try:
                    await run
                except (asyncio.CancelledError, Exception):
                    pass
            self._detach()
            for dev in reversed(started):
                try:
                    await dev.stop()
                except Exception as e:
                    log.debug("closing audio: %s", e)

    async def _watch(self, run: asyncio.Task, source: Any) -> None:
        """Return when the voice loop ends or the microphone stops delivering audio (unplugged)."""
        while True:
            done, _ = await asyncio.wait({run}, timeout=1.0)
            if done:
                if not run.cancelled() and run.exception():
                    raise run.exception()
                log.info("voice loop ended")
                return
            if self.watch_frames:
                quiet = time.monotonic() - getattr(source, "last_frame", time.monotonic())
                if quiet > self.silence_s:
                    log.info("no audio from the microphone for %.0fs: treating it as unplugged", quiet)
                    return

    # ------------------------------------------------------------------ bookkeeping
    def _attach(self) -> None:
        self.attached, self.note = True, ""
        self.attaches += 1
        self.attention.front, self.attention.leds = self.pipeline, self.leds
        if self.leds is not None:
            ring = getattr(self.leds, "ring", None)
            if ring is not None:                       # forget failures from while it was unplugged
                ring._failures, ring._disabled_until = 0, 0.0
            try:
                self.leds.start()
                self.leds.reapply()
            except Exception as e:
                log.debug("LED start: %s", e)
        self.attention._leds()
        store.put("state", "idle")
        store.record("system", "Microphone connected: voice is on", voice=True, transport=self.settings.audio_transport)
        log.info("voice on")

    def _detach(self) -> None:
        was = self.attached
        self.attached = False
        self.attention.front, self.attention.leds = None, None     # nothing is spoken; approvals wait in the dashboard
        if self.pipeline is not None:
            self.pipeline.abandon()
        if was:
            self.note = WAITING
            store.put("state", "idle")
            store.record("system", "Microphone disconnected: text only", voice=False)
            log.info("voice off: microphone disconnected")

    def close(self) -> None:
        """At shutdown: put the LED ring back the way it was."""
        if self.leds is not None:
            try:
                self.leds.stop()
            except Exception as e:
                log.debug("LED stop: %s", e)
