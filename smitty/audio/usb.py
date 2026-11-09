"""USB implementation of AudioSource / AudioSink using the reSpeaker XVF3800.

The XVF3800 (USB firmware) exposes a 2-in / 2-out, 16 kHz USB audio device.
Input channel 0 is the processed signal (beamformed, AEC'd, noise-suppressed),
which is what we feed to the pipeline. Output goes to the speaker wired to the
board; the XVF3800 uses it as the echo-cancellation reference, which is why the
mic AND the speaker must both go through this device.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from typing import AsyncIterator, Optional

import numpy as np
import sounddevice as sd

from smitty.audio.base import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    AudioSink,
    AudioSource,
    resample,
    to_float32,
)

log = logging.getLogger(__name__)

RESPEAKER_NAME = "reSpeaker XVF3800"


def find_device(name_substring: str = RESPEAKER_NAME) -> Optional[int]:
    """Return the sounddevice index of the first device whose name matches."""
    for idx, dev in enumerate(sd.query_devices()):
        if name_substring.lower() in dev["name"].lower():
            return idx
    return None


def describe_devices() -> str:
    return str(sd.query_devices())


def refresh_devices() -> None:
    """Make PortAudio notice devices plugged in or removed since it started.

    PortAudio lists devices once; re-initialising is the only way to rescan. Only call
    this while no stream is open.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:
        log.debug("could not rescan audio devices: %s", e)


async def _close_stream(stream) -> None:
    """Close a stream off the event loop, giving up if an unplugged device makes it hang."""
    def close() -> None:
        try:
            stream.abort(ignore_errors=True)
            stream.close(ignore_errors=True)
        except Exception as e:
            log.debug("closing audio stream: %s", e)
    try:
        await asyncio.wait_for(asyncio.to_thread(close), timeout=3.0)
    except asyncio.TimeoutError:
        log.warning("audio stream did not close (device gone?); leaving it behind")


class UsbAudioSource(AudioSource):
    def __init__(
        self,
        device: int | str | None = None,
        channel: int = 0,
        queue_frames: int = 64,
    ):
        self._device = device if device is not None else find_device()
        if self._device is None:
            raise RuntimeError(f"{RESPEAKER_NAME} not found; is it plugged in?")
        self._channel = channel
        self._q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_frames)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: sd.InputStream | None = None
        self._dropped = 0
        self.last_frame = time.monotonic()      # when audio last arrived; goes stale if the device is unplugged

    def _callback(self, indata, frames, time_info, status):
        # Runs on PortAudio's thread. Never block here.
        if status:
            log.debug("input status: %s", status)
        frame = np.ascontiguousarray(indata[:, self._channel]).copy()
        try:
            self._loop.call_soon_threadsafe(self._enqueue, frame)
        except RuntimeError:
            pass  # loop closed

    def _enqueue(self, frame: np.ndarray) -> None:
        # Runs on the event loop. If the consumer is busy (model loading, TTS),
        # drop the oldest frame rather than blocking or raising.
        self.last_frame = time.monotonic()
        if self._q.full():
            self._q.get_nowait()
            self._dropped += 1
            if self._dropped % 50 == 1:
                log.debug("input queue full; dropping oldest (%d dropped so far)", self._dropped)
        self._q.put_nowait(frame)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        info = sd.query_devices(self._device)
        n_in = int(info["max_input_channels"])
        self._stream = sd.InputStream(
            device=self._device,
            samplerate=SAMPLE_RATE,
            channels=min(2, n_in),
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=self._callback,
        )
        self._stream.start()
        self.last_frame = time.monotonic()
        log.info("USB source started on %r (%d ch)", info["name"], n_in)

    async def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            await _close_stream(stream)

    async def frames(self) -> AsyncIterator[np.ndarray]:
        while self._stream is not None:
            yield await self._q.get()

    @property
    def backlog(self) -> int:
        return self._q.qsize()

    def drain(self) -> int:
        """Discard buffered frames (e.g. audio that arrived while we were busy)."""
        n = 0
        while not self._q.empty():
            self._q.get_nowait()
            n += 1
        return n


class UsbAudioSink(AudioSink):
    """Streams PCM to the reSpeaker's speaker output via a persistent OutputStream.

    A persistent stream (rather than sd.play) means no open/close click per
    utterance and sub-10ms latency for interrupts: ``interrupt()`` just empties
    the buffer and the callback outputs silence.
    """

    def __init__(self, device: int | str | None = None, blocksize: int = 1024, volume: float = 1.0):
        self._device = device if device is not None else find_device()
        if self._device is None:
            raise RuntimeError(f"{RESPEAKER_NAME} not found; is it plugged in?")
        self._blocksize = blocksize
        self.volume = float(volume)  # master gain applied to everything played
        self._buf: queue.Queue[np.ndarray] = queue.Queue()
        self._pending = np.zeros(0, dtype=np.float32)
        self._stream: sd.OutputStream | None = None
        self._n_out = 2
        self._playing = False
        self._done: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _callback(self, outdata, frames, time_info, status):
        if status:
            log.debug("output status: %s", status)
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        while filled < frames:
            if len(self._pending) == 0:
                try:
                    self._pending = self._buf.get_nowait()
                except queue.Empty:
                    break
                if self._pending is None:  # end-of-utterance marker
                    self._pending = np.zeros(0, dtype=np.float32)
                    self._signal_done()
                    continue
            take = min(frames - filled, len(self._pending))
            out[filled : filled + take] = self._pending[:take]
            self._pending = self._pending[take:]
            filled += take
        outdata[:] = out[:, None].repeat(self._n_out, axis=1)

    def _signal_done(self):
        self._playing = False
        if self._loop and self._done:
            self._loop.call_soon_threadsafe(self._done.set)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        info = sd.query_devices(self._device)
        self._n_out = min(2, int(info["max_output_channels"]))
        self._stream = sd.OutputStream(
            device=self._device,
            samplerate=SAMPLE_RATE,
            channels=self._n_out,
            dtype="float32",
            blocksize=self._blocksize,
            callback=self._callback,
        )
        self._stream.start()
        log.info("USB sink started on %r (%d ch)", info["name"], self._n_out)

    async def stop(self) -> None:
        self.interrupt()
        stream, self._stream = self._stream, None
        if stream is not None:
            await _close_stream(stream)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def interrupt(self) -> None:
        while True:
            try:
                self._buf.get_nowait()
            except queue.Empty:
                break
        self._pending = np.zeros(0, dtype=np.float32)
        self._signal_done()

    async def play(self, pcm: np.ndarray, sample_rate: int) -> None:
        if self._stream is None:
            raise RuntimeError("sink not started")
        audio = resample(to_float32(np.asarray(pcm).reshape(-1)), sample_rate, SAMPLE_RATE)
        if len(audio) == 0:
            return
        if self.volume != 1.0:
            audio = audio * self.volume
        self._done = asyncio.Event()
        self._playing = True
        # Chunk so interrupt() can cut mid-utterance quickly.
        step = self._blocksize * 4
        for i in range(0, len(audio), step):
            self._buf.put(audio[i : i + step])
        self._buf.put(None)
        try:
            await self._done.wait()
        except asyncio.CancelledError:
            self.interrupt()
            raise
