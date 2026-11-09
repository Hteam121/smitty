"""Speech-to-text with mlx-whisper (runs on the Apple GPU via MLX)."""

from __future__ import annotations

import asyncio
import logging
import re
import time

import numpy as np

from smitty.audio.base import to_float32

log = logging.getLogger(__name__)

# Things Whisper says when it hears nothing. Case-insensitive, full-match.
HALLUCINATIONS = re.compile(
    r"^\W*(thank you|thanks for watching|thank you for watching|you|bye|music|"
    r"\[.*?\]|\(.*?\)|subtitles? by .*|please subscribe.*)\W*$",
    re.IGNORECASE,
)


class WhisperSTT:
    def __init__(self, model: str = "mlx-community/whisper-large-v3-turbo", language: str = "en"):
        import mlx_whisper  # heavy

        self._mlx_whisper = mlx_whisper
        self.model = model
        self.language = language

    def transcribe_sync(self, audio: np.ndarray) -> str:
        t0 = time.monotonic()
        result = self._mlx_whisper.transcribe(
            to_float32(audio),
            path_or_hf_repo=self.model,
            language=self.language,
            fp16=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        if "segments" in result:
            kept = [
                seg for seg in result["segments"]
                if seg.get("no_speech_prob", 0.0) < 0.6 or seg.get("avg_logprob", -1.0) > -0.5
            ]
            if len(kept) < len(result["segments"]):
                log.debug("dropped %d low-confidence segment(s)", len(result["segments"]) - len(kept))
            text = " ".join(seg["text"].strip() for seg in kept).strip()
        else:
            text = (result.get("text") or "").strip()
        if HALLUCINATIONS.match(text):
            log.info("dropping likely hallucination %r", text)
            text = ""
        log.info("transcribed %.1fs of audio in %.2fs: %r", len(audio) / 16000, time.monotonic() - t0, text)
        return text

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.to_thread(self.transcribe_sync, audio)

    def warm_up(self) -> None:
        """Load weights + compile kernels so the first real request is fast."""
        self.transcribe_sync(np.zeros(16000, dtype=np.float32))
