"""Transport-agnostic audio interfaces.

Two implementations:
  * ``smitty.audio.usb``        - this Mac's sound devices: the reSpeaker XVF3800 over USB, or the
                                  Mac's own microphone and speakers
  * ``smitty.audio.websocket``  - XIAO ESP32S3 streaming PCM over Wi-Fi

Contract shared by both:
  * Audio is 16 kHz, mono, signed 16-bit PCM, little-endian (what openWakeWord,
    Silero VAD and Whisper all want, and the XVF3800's native rate).
  * A source yields fixed-size frames of FRAME_SAMPLES samples (80 ms). That's
    openWakeWord's natural chunk; VAD re-chunks internally if it needs smaller.
  * A sink accepts arbitrary-length PCM at any sample rate and is responsible
    for resampling to whatever the transport needs. ``stop()`` must cut playback
    immediately (used for barge-in / interrupt).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 80
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 1280


class AudioSource(ABC):
    """Produces 16 kHz mono int16 frames."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def frames(self) -> AsyncIterator[np.ndarray]:
        """Async iterator of np.int16 arrays, each exactly FRAME_SAMPLES long."""
        ...

    async def __aenter__(self) -> "AudioSource":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()


class AudioSink(ABC):
    """Plays PCM audio. Implementations resample to their transport's rate."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None:
        """Flush/close. Playback in progress is cut."""
        ...

    @abstractmethod
    async def play(self, pcm: np.ndarray, sample_rate: int) -> None:
        """Play ``pcm`` (int16 or float32 in [-1, 1], mono) and return when done.

        Must be cancellable: if the awaiting task is cancelled, playback stops.
        """
        ...

    @abstractmethod
    def interrupt(self) -> None:
        """Cut current playback immediately (thread-safe, non-blocking)."""
        ...

    @property
    @abstractmethod
    def is_playing(self) -> bool: ...

    async def __aenter__(self) -> "AudioSink":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()


def to_int16(pcm: np.ndarray) -> np.ndarray:
    """Normalise float32/float64 [-1, 1] or int16 input to int16."""
    if pcm.dtype == np.int16:
        return pcm
    if np.issubdtype(pcm.dtype, np.floating):
        return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    raise TypeError(f"unsupported pcm dtype {pcm.dtype}")


def to_float32(pcm: np.ndarray) -> np.ndarray:
    if pcm.dtype == np.float32:
        return pcm
    if pcm.dtype == np.int16:
        return pcm.astype(np.float32) / 32768.0
    if np.issubdtype(pcm.dtype, np.floating):
        return pcm.astype(np.float32)
    raise TypeError(f"unsupported pcm dtype {pcm.dtype}")


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resampler. Good enough for speech; no scipy needed."""
    if src_rate == dst_rate:
        return pcm
    x = to_float32(pcm)
    n_out = int(round(len(x) * dst_rate / src_rate))
    if n_out == 0:
        return np.zeros(0, dtype=np.float32)
    src_t = np.arange(len(x), dtype=np.float64)
    dst_t = np.linspace(0, len(x) - 1, n_out, dtype=np.float64)
    return np.interp(dst_t, src_t, x).astype(np.float32)
