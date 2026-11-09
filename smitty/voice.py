"""Voice that comes and goes with the microphone.

Smitty's brain, scheduler, dashboard and chat run the whole time. ``VoiceLink`` adds the
voice side (wake word, speech-to-text, speech) whenever a microphone is there and takes it
away again when it goes, without restarting anything, so the conversation carries on across
the change. ``LocalAudio`` decides which microphone and speaker that is: the reSpeaker when
it is plugged in, otherwise the Mac's own.

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
NO_MAC_MIC = "No microphone found on this Mac: connect one, or plug in the reSpeaker, for voice"
NO_MAC_SPEAKER = "No speaker found on this Mac for Smitty's voice"
MIC_BLOCKED = ("no sound is arriving. Allow the microphone for the app Smitty runs in (Terminal, or Claude) "
               "in System Settings > Privacy & Security > Microphone, then start Smitty again")
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


class LocalAudio:
    """Chooses the microphone and speaker on this Mac.

    mode "auto": the reSpeaker when it is plugged in, else the Mac's own microphone and speakers.
    mode "mac": always the Mac's own.  mode "respeaker": only the reSpeaker (voice waits for it).
    """

    def __init__(self, settings):
        self.settings = settings
        mode = str(settings.audio_transport).lower()
        self.mode = "respeaker" if mode in ("usb", "respeaker") else "mac" if mode == "mac" else "auto"
        self.device: str | None = None        # "respeaker" or "mac": what the last check chose
        self.label = ""                       # the same in plain words, for the dashboard
        self.waiting = WAITING if self.mode == "respeaker" else NO_MAC_MIC
        self._mic: int | None = None
        self._speaker: int | None = None
        self._board_without_audio = False     # the board is on USB but offers no microphone (Wi-Fi firmware)

    def microphone(self) -> tuple[bool, str]:
        """(ready, note). Rescans the Mac's devices, so only call it while no audio stream is open."""
        from smitty.audio.usb import device_name, find_device, mac_devices, refresh_devices
        if self.mode == "respeaker":
            ready, note = usb_microphone()
            if ready:
                self.device, self.label = "respeaker", "reSpeaker XVF3800 over USB"
            return ready, note
        seen = usb_board_seen() if self.mode == "auto" else False
        refresh_devices()
        if self.mode == "auto" and find_device() is not None:
            self.device, self.label, self._board_without_audio = "respeaker", "reSpeaker XVF3800 over USB", False
            return True, ""
        self._board_without_audio = bool(seen)
        self._mic, self._speaker = mac_devices(self.settings.mic, self.settings.speaker)
        if self._mic is None:
            return False, (f"No microphone named {self.settings.mic!r} on this Mac" if self.settings.mic else NO_MAC_MIC)
        if self._speaker is None:
            return False, (f"No speaker named {self.settings.speaker!r} on this Mac" if self.settings.speaker else NO_MAC_SPEAKER)
        self.device = "mac"
        self.label = f"{device_name(self._mic)} and {device_name(self._speaker)}"
        return True, ""

    def make(self) -> tuple[Any, Any]:
        """The (source, sink) for what ``microphone`` chose; checks first when nothing was chosen yet."""
        from smitty.audio.usb import UsbAudioSink, UsbAudioSource
        if self.device is None:
            ready, note = self.microphone()
            if not ready:
                raise RuntimeError(note)
        if self.device == "respeaker":
            return UsbAudioSource(), UsbAudioSink(volume=self.settings.volume)
        source = UsbAudioSource(device=self._mic)
        source.has_ring = False
        # The Mac's speakers can be heard by its microphone: wait out the tail of each utterance.
        return source, UsbAudioSink(device=self._speaker, volume=self.settings.volume, settle_s=0.25)

    def better_available(self) -> bool:
        """While voice runs on the Mac's own audio: has the reSpeaker been plugged in? Safe during a stream."""
        if self.mode != "auto" or self.device != "mac":
            return False
        if not usb_board_seen():
            self._board_without_audio = False
            return False
        return not self._board_without_audio


class VoiceLink:
    def __init__(self, settings, brain, attention,
                 make_audio: Callable[[], tuple[Any, Any]],
                 microphone: Callable[[], tuple[bool, str]],
                 make_pipeline: Callable[[Any, Any, Any], Any],
                 make_leds: Callable[[Any], Any] | None = None,
                 poll_s: float = 3.0, silence_s: float = 4.0, watch_frames: bool = True,
                 greeting: str = "", waiting: str = WAITING,
                 better: Callable[[], bool] | None = None, label: Callable[[], str] | None = None):
        self.settings, self.brain, self.attention = settings, brain, attention
        self.make_audio, self.microphone = make_audio, microphone
        self.make_pipeline, self.make_leds = make_pipeline, make_leds
        self.poll_s, self.silence_s, self.watch_frames = poll_s, silence_s, watch_frames
        self.greeting = greeting
        self.waiting = waiting               # what to say while there is no microphone
        self.better = better                 # "a preferred microphone has appeared": checked while serving
        self.label = label or (lambda: "")   # the microphone and speaker in use, in plain words
        self.pipeline: Any = None            # built on first attach, then reused
        self.leds: Any = None                # the LED ring, while the audio in use has one
        self._ring: Any = None               # the ring's driver, made the first time it is needed
        self.source: Any = None
        self.attached = False
        self.note = waiting                  # why voice is off, in plain words (shown in the dashboard)
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

    def _leds_for(self, source: Any) -> Any:
        """The LED ring when this microphone has one (the reSpeaker does, the Mac's own does not)."""
        if self.make_leds is None or not getattr(source, "has_ring", True):
            return None
        if self._ring is None:
            self._ring = self.make_leds(source)
        return self._ring

    @property
    def mic_blocked(self) -> bool:
        """Voice is on but only silence has arrived: macOS is withholding the microphone."""
        s = self.source
        return bool(self.attached and s is not None and not getattr(s, "heard", True)
                    and time.monotonic() - getattr(s, "started_at", time.monotonic()) > 6)

    async def _serve(self) -> None:
        source, sink = self.make_audio()
        self.source = source
        self.leds = self._leds_for(source)
        if self.pipeline is None:
            log.info("microphone found: loading the speech models (first time only)...")
            self.note = "Microphone found: loading the speech models"
            self.pipeline = await asyncio.to_thread(self.make_pipeline, source, sink, self.leds)
            if hasattr(self.pipeline.stt, "warm_up"):
                await asyncio.to_thread(self.pipeline.stt.warm_up)
        else:
            from smitty.pipeline import LogStatus
            self.pipeline.source, self.pipeline.sink = source, sink
            self.pipeline.status = self.leds or LogStatus()
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
            await self._watch(run, source, sink)
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

    async def _watch(self, run: asyncio.Task, source: Any, sink: Any = None) -> None:
        """Return when the voice loop ends, the microphone or speaker stops responding (unplugged),
        or a preferred microphone has been plugged in."""
        checked = time.monotonic()
        while True:
            done, _ = await asyncio.wait({run}, timeout=1.0)
            if done:
                if not run.cancelled() and run.exception():
                    raise run.exception()
                log.info("voice loop ended")
                return
            now = time.monotonic()
            if self.watch_frames:
                quiet = now - getattr(source, "last_frame", now)
                if quiet > self.silence_s:
                    log.info("no audio from the microphone for %.0fs: treating it as unplugged", quiet)
                    return
                if now - getattr(sink, "last_frame", now) > self.silence_s:
                    log.info("the speaker stopped responding: treating it as disconnected")
                    return
            if self.better is not None and now - checked >= self.poll_s:
                checked = now
                idle = self.pipeline is None or self.pipeline.state.value == "idle"
                if idle and await asyncio.to_thread(self.better):      # never in the middle of a conversation
                    log.info("the reSpeaker was plugged in: switching to it")
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
        using = self.label()
        store.record("system", "Voice is on" + (f": {using}" if using else ""), voice=True,
                     transport=self.settings.audio_transport, audio=using)
        log.info("voice on%s", f" ({using})" if using else "")

    def _detach(self) -> None:
        was = self.attached
        self.attached = False
        self.attention.front, self.attention.leds = None, None     # nothing is spoken; approvals wait in the dashboard
        if self.pipeline is not None:
            self.pipeline.abandon()
        if was:
            self.note = self.waiting
            store.put("state", "idle")
            store.record("system", "Microphone disconnected: text only", voice=False)
            log.info("voice off: microphone disconnected")

    def close(self) -> None:
        """At shutdown: put the LED ring back the way it was."""
        if self._ring is not None:
            try:
                self._ring.stop()
            except Exception as e:
                log.debug("LED stop: %s", e)
