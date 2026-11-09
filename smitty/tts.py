"""Text-to-speech. Kokoro (ONNX) preferred; macOS `say` as the fallback."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class TTS(ABC):
    @abstractmethod
    def synthesize_sync(self, text: str) -> tuple[np.ndarray, int]:
        """Return (float32 mono samples, sample_rate)."""

    async def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        return await asyncio.to_thread(self.synthesize_sync, text)


class KokoroTTS(TTS):
    def __init__(self, model_path: Path, voices_path: Path, voice: str = "bm_george", speed: float = 1.0):
        from kokoro_onnx import Kokoro  # heavy

        self._k = Kokoro(str(model_path), str(voices_path))
        self.voice = voice
        self.speed = speed

    def synthesize_sync(self, text: str) -> tuple[np.ndarray, int]:
        t0 = time.monotonic()
        samples, sr = self._k.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        log.info("kokoro synthesized %d chars in %.2fs", len(text), time.monotonic() - t0)
        return np.asarray(samples, dtype=np.float32), int(sr)


class MacSayTTS(TTS):
    """Fallback using the built-in macOS `say` command. Always available."""

    def __init__(self, voice: str = "Daniel", rate_wpm: int = 190):
        self.voice = voice
        self.rate_wpm = rate_wpm

    def synthesize_sync(self, text: str) -> tuple[np.ndarray, int]:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "say.wav"
            subprocess.run(
                [
                    "say", "-v", self.voice, "-r", str(self.rate_wpm),
                    "-o", str(out), "--file-format=WAVE", "--data-format=LEI16@22050",
                    text,
                ],
                check=True, capture_output=True,
            )
            with wave.open(str(out), "rb") as w:
                sr = w.getframerate()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                if w.getnchannels() > 1:
                    pcm = pcm.reshape(-1, w.getnchannels())[:, 0]
        return pcm.astype(np.float32) / 32768.0, sr


def make_tts(settings) -> TTS:
    if settings.tts_engine == "kokoro":
        try:
            return KokoroTTS(
                settings.kokoro_model_path, settings.kokoro_voices_path,
                voice=settings.kokoro_voice, speed=settings.kokoro_speed,
            )
        except Exception as e:  # missing package or model files
            log.warning("Kokoro unavailable (%s); falling back to macOS say", e)
    return MacSayTTS(voice=settings.say_voice)
