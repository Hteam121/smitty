"""The main voice loop: wake word -> listen -> transcribe -> brain -> speak.

Besides user-initiated turns, two things need the microphone/speaker from
outside the loop: worker sessions asking for a spoken yes/no (``confirm``) and
events to announce (``announce``). Both are queued and handled by the loop
between wake-word checks, so only one thing ever talks or listens at a time.

The brain is any object with ``async ask(text) -> str`` and optionally
``async announce(event) -> str``. Status changes go to a StatusListener (LED
ring later; logging for now).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

import numpy as np

from smitty import store
from smitty.audio.base import SAMPLE_RATE, AudioSink, AudioSource
from smitty.brain.safety import parse_yes_no, wants_all
from smitty.config import Settings
from smitty.stt import WhisperSTT
from smitty.tts import TTS
from smitty.vad import EndpointDetector, SileroVAD
from smitty.wake import WakeWordDetector

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ERROR = "error"


class StatusListener(Protocol):
    def on_state(self, state: State) -> None: ...


class LogStatus:
    def on_state(self, state: State) -> None:
        log.info("state -> %s", state.value)


class EchoBrain:
    async def ask(self, text: str) -> str:
        return f"You said: {text}"

    async def announce(self, event: dict[str, Any]) -> str:
        return str(event.get("summary") or event)


@dataclass
class _Request:
    kind: str                      # "confirm" | "announce" | "say"
    payload: Any
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class LoopWatchdog:
    """Logs when the asyncio loop is blocked (a sync call hogging it), with the main
    thread's stack at that moment, so stalls that delay audio can be traced."""

    def __init__(self, loop: asyncio.AbstractEventLoop, threshold_s: float = 0.4):
        import threading
        self._loop, self._threshold = loop, threshold_s
        self._main_ident = threading.main_thread().ident
        self._last_pong = time.monotonic()
        self._reported = False
        self._thread = threading.Thread(target=self._run, name="loop-watchdog", daemon=True)
        self._thread.start()

    def _pong(self) -> None:
        self._last_pong = time.monotonic()

    def _run(self) -> None:
        import sys
        import traceback
        while True:
            time.sleep(0.2)
            try:
                self._loop.call_soon_threadsafe(self._pong)
            except RuntimeError:
                return  # loop closed
            stalled = time.monotonic() - self._last_pong
            if stalled > self._threshold and not self._reported:
                self._reported = True
                frame = sys._current_frames().get(self._main_ident)
                where = "".join(traceback.format_stack(frame)[-4:]) if frame else "?"
                log.warning("event loop blocked for %.1fs; main thread at:\n%s", stalled, where)
            elif stalled <= self._threshold and self._reported:
                self._reported = False
                log.warning("event loop responsive again")


def _chime(freq: float = 880.0, ms: int = 120, volume: float = 0.25) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * ms / 1000)) / SAMPLE_RATE
    env = np.minimum(1.0, np.minimum(t / 0.01, (t[-1] - t) / 0.03))
    return (volume * env * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class VoicePipeline:
    def __init__(self, settings: Settings, source: AudioSource, sink: AudioSink, brain: Any,
                 status: StatusListener | None = None):
        self.settings = settings
        self.source = source
        self.sink = sink
        self.brain = brain
        self.status = status or LogStatus()
        self.state = State.IDLE
        self.tts: TTS | None = None  # set by caller (make_tts) to keep this import-light
        self._requests: asyncio.Queue[_Request] = asyncio.Queue()
        self._current: _Request | None = None      # the request being handled right now
        self._speech_lock = asyncio.Lock()

        log.info("loading models...")
        self.wake = WakeWordDetector(settings.wake_model, settings.wake_threshold,
                                     settings.wake_refractory_s, settings.wake_patience)
        self.endpoint = EndpointDetector(
            SileroVAD(settings.vad_model_path),
            threshold=settings.vad_threshold,
            silence_ms=settings.vad_silence_ms,
            no_speech_timeout_ms=settings.vad_no_speech_timeout_ms,
            max_utterance_ms=settings.vad_max_utterance_ms,
            pre_roll_ms=settings.vad_pre_roll_ms,
            min_speech_ms=settings.vad_min_speech_ms,
        )
        self.stt = WhisperSTT(settings.whisper_model, settings.whisper_language)
        self._recent = deque(maxlen=25)  # last 2 s of frames, for wake verification
        self._verify_re = re.compile(settings.wake_verify_pattern, re.I)

    # ------------------------------------------------------------------ public API for other tasks
    async def confirm(self, question: str) -> bool | str | None:
        """Ask the user a yes/no question by voice.

        Returns True, False, None (no clear answer), or the string "all" when the user
        said yes and asked not to be asked again ("yes, allow everything"). "all" is truthy.
        """
        req = _Request("confirm", question)
        await self._requests.put(req)
        return await req.future

    async def announce(self, event: dict[str, Any]) -> str:
        """Phrase an event with the brain and speak it. Returns what was said."""
        req = _Request("announce", event)
        await self._requests.put(req)
        return await req.future or ""

    async def say(self, text: str) -> None:
        """Synthesize and play text. Serialised so announcements don't overlap."""
        if not text:
            return
        async with self._speech_lock:
            prev = self.state
            self._set_state(State.SPEAKING)
            try:
                pcm, sr = await self.tts.synthesize(text)
                await self.sink.play(pcm, sr)
            finally:
                self._set_state(prev if prev == State.NEEDS_CONFIRMATION else State.IDLE)

    # ------------------------------------------------------------------ internals
    def _set_state(self, state: State) -> None:
        if state == self.state:
            return
        self.state = state
        self.status.on_state(state)
        store.put("state", state.value)

    async def _capture(self, frames: AsyncIterator[np.ndarray], no_speech_timeout_ms: int | None = None,
                       chime: bool = True) -> str | None:
        """Listen for one utterance and transcribe it. None if nothing usable was said."""
        self._set_state(State.LISTENING)
        self.endpoint.reset()
        t_start = time.monotonic()
        # Audio that arrived while we were speaking/thinking is stale: drop it so the
        # no-speech timeout counts from now, not from several seconds ago.
        stale = self.source.drain() if hasattr(self.source, "drain") else 0
        if stale:
            log.info("listening: dropped %d stale frames (%.1fs)", stale, stale * 0.08)
        default_timeout = self.endpoint.no_speech_timeout_ms
        if no_speech_timeout_ms is not None:
            self.endpoint.no_speech_timeout_ms = no_speech_timeout_ms
        if chime:
            await self.sink.play(_chime(), SAMPLE_RATE)
        try:
            async for frame in frames:
                utt = self.endpoint.feed(frame)
                if utt is not None:
                    break
            else:
                return None
        except TimeoutError:
            log.info("no speech; giving up listening after %.1fs wall (%d ms of audio)",
                     time.monotonic() - t_start, self.endpoint._elapsed_ms)
            return None
        finally:
            self.endpoint.no_speech_timeout_ms = default_timeout

        self._set_state(State.THINKING)
        log.info("utterance captured: %.1fs (%d ms speech)", utt.duration_s, utt.speech_ms)
        text = await self.stt.transcribe(utt.audio)
        return text or None

    def abandon(self) -> None:
        """The microphone went away mid-flight: answer everyone still waiting so nothing hangs.

        A confirmation gets None ("no answer", so it stays open in the dashboard); an
        announcement gets None (it was not spoken). Safe to call when nothing is waiting.
        """
        waiting = [self._current] if self._current is not None else []
        while not self._requests.empty():
            waiting.append(self._requests.get_nowait())
        for req in waiting:
            if not req.future.done():
                req.future.set_result(None)
        self._current = None
        self.state = State.IDLE

    async def _handle_request(self, frames: AsyncIterator[np.ndarray], req: _Request) -> None:
        if req.future.done():
            log.info("skipping %s request: the caller stopped waiting", req.kind)
            return
        self._current = req
        try:
            if req.kind == "say":
                await self.say(req.payload)
                req.future.set_result(None)
            elif req.kind == "announce":
                text = await self.brain.announce(req.payload) if hasattr(self.brain, "announce") else str(req.payload)
                store.record("said", text, source="announcement",
                             about=(req.payload or {}).get("title") if isinstance(req.payload, dict) else None)
                await self.say(text)
                req.future.set_result(text)
            elif req.kind == "confirm":
                self._set_state(State.NEEDS_CONFIRMATION)
                await self.say(req.payload)
                answer = None
                for attempt in range(2):
                    text = await self._capture(frames, no_speech_timeout_ms=8000, chime=True)
                    self._set_state(State.NEEDS_CONFIRMATION)
                    log.info("confirmation answer: %r", text)
                    answer = parse_yes_no(text) if text else None
                    if answer is True and wants_all(text):
                        answer = "all"
                    if answer is not None or attempt == 1 or req.future.done():
                        break
                    await self.say("Sorry, was that a yes or a no?")
                if req.future.done():
                    log.warning("confirmation answered too late; the caller already gave up")
                    return
                if answer is None:
                    await self.say("No clear answer, so I'll leave that one.")
                elif answer == "all":
                    await self.say("Okay, I won't ask again for this session.")
                store.record("confirm", req.payload, heard=text,
                             answer={True: "yes", False: "no", None: "no clear answer", "all": "yes to all"}.get(answer, str(answer)))
                req.future.set_result(answer)
        except Exception as e:
            log.exception("request %s failed", req.kind)
            if not req.future.done():
                req.future.set_exception(e)
        finally:
            if self._current is req and req.future.done():
                self._current = None          # still set after a cancel, so abandon() can answer it
            self._set_state(State.IDLE)

    async def _converse(self, frames: AsyncIterator[np.ndarray], text: str) -> None:
        """One user turn plus any follow-ups while the brain keeps asking questions."""
        while True:
            self._set_state(State.THINKING)
            store.record("heard", text)
            t_brain = time.monotonic()
            reply = await self.brain.ask(text)
            store.record("said", reply, source="reply", seconds=round(time.monotonic() - t_brain, 1))
            await self.say(reply)
            if not reply.rstrip().endswith("?"):
                return
            # Smitty asked something: listen for the answer without a wake word.
            if hasattr(self.source, "drain"):
                self.source.drain()
            text = await self._capture(frames, no_speech_timeout_ms=self.settings.follow_up_timeout_ms, chime=True)
            if text is None:
                return

    async def _verify_wake(self) -> bool:
        """Decide whether a wake-word hit was really someone saying the wake word.

        Two cheap checks on the last 2 s of audio (see Settings.wake_verify):
          1. Silero VAD must have seen speech in it; pure noise can't be a wake word.
          2. In "strict" mode Whisper transcribes it and the text must contain a
             smitty-like word. An empty transcript counts as a false wake.
        "log" only logs the transcript (in the background); "off" skips both.
        """
        mode = self.settings.wake_verify
        if mode == "off" or len(self._recent) < 10:
            return True
        audio = np.concatenate(list(self._recent))

        vad = SileroVAD(self.settings.vad_model_path) if not hasattr(self, "_wake_vad") else self._wake_vad
        self._wake_vad = vad
        vad.reset()
        speech = max(vad.process(f) for f in list(self._recent)[-19:])  # last ~1.5 s
        score = round(float(getattr(self.wake, "last_score", 0.0)), 2)
        if speech < self.settings.wake_vad_threshold:
            log.info("wake rejected: no speech in trigger window (vad %.2f)", speech)
            store.record("wake", "Ignored: just noise", accepted=False, reason="no speech", score=score, vad=round(speech, 2))
            return False

        async def transcribe_and_log() -> str:
            text = await self.stt.transcribe(audio)
            log.info("wake trigger heard as %r (vad %.2f)", text, speech)
            return text

        if mode == "strict":
            text = await transcribe_and_log()
            if not text or not self._verify_re.search(text):
                log.info("wake rejected: trigger text lacks the wake word")
                store.record("wake", "Ignored: not the wake word", accepted=False, reason="other speech",
                             heard=text, score=score, vad=round(speech, 2))
                return False
            store.record("wake", "Wake word", accepted=True, heard=text, score=score, vad=round(speech, 2))
            return True
        store.record("wake", "Wake word", accepted=True, score=score, vad=round(speech, 2))
        asyncio.create_task(transcribe_and_log())
        return True

    async def run(self) -> None:
        self._set_state(State.IDLE)
        self.wake.reset()
        if getattr(self, "_watchdog", None) is None:        # run() is called again each time the mic returns
            self._watchdog = LoopWatchdog(asyncio.get_running_loop())
        frames = self.source.frames()
        log.info("ready: say %r", self.settings.wake_model.replace("_", " "))
        n = 0
        async for frame in frames:
            n += 1
            self._recent.append(frame)
            if n % 250 == 0:  # heartbeat every 20 s of audio
                log.debug("alive: %d frames, backlog %d, state %s", n, getattr(self.source, "backlog", -1), self.state.value)
            pending = None if self._requests.empty() else self._requests.get_nowait()
            if pending is None and (self.state != State.IDLE or not self.wake.detect(frame)):
                continue  # the common case: idle, nothing queued, no wake word in this frame
            try:
                if pending is not None:
                    await self._handle_request(frames, pending)
                else:
                    if not await self._verify_wake():
                        continue
                    text = await self._capture(frames)
                    if text is not None:
                        await self._converse(frames, text)
            except Exception:
                log.exception("pipeline error")
                self._set_state(State.ERROR)
                await asyncio.sleep(0.5)
            finally:
                # Only after an interaction: forget audio from while we were busy.
                self.wake.reset()
                if hasattr(self.source, "drain"):
                    self.source.drain()
                self._set_state(State.IDLE)
