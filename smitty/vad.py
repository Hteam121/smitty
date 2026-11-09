"""Silero VAD (v5 ONNX, via onnxruntime) + an end-of-utterance detector.

We run the ONNX model directly instead of the `silero-vad` pip package so we
don't need to pull in PyTorch just for VAD.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from smitty.audio.base import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

CHUNK = 512          # samples per VAD step at 16 kHz (32 ms)
CONTEXT = 64         # v5 prepends the last 64 samples of the previous chunk


class SileroVAD:
    def __init__(self, model_path: Path):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._carry = np.zeros(0, dtype=np.float32)

    def _step(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self._context, chunk])[None, :].astype(np.float32)
        out, self._state = self._sess.run(
            None,
            {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
        )
        self._context = chunk[-CONTEXT:]
        return float(out[0, 0])

    def process(self, frame: np.ndarray) -> float:
        """Feed an int16 frame of any length; return max speech probability over it."""
        audio = frame.astype(np.float32) / 32768.0
        buf = np.concatenate([self._carry, audio])
        probs = []
        i = 0
        while i + CHUNK <= len(buf):
            probs.append(self._step(buf[i : i + CHUNK]))
            i += CHUNK
        self._carry = buf[i:]
        return max(probs) if probs else 0.0


@dataclass
class Utterance:
    audio: np.ndarray  # int16, 16 kHz
    duration_s: float
    speech_ms: int     # frames the VAD flagged as speech


class EndpointDetector:
    """Collects frames after the wake word until the user stops talking.

    States: waiting (no speech yet) -> speaking -> done / timeout.
    """

    def __init__(
        self,
        vad: SileroVAD,
        threshold: float = 0.5,
        silence_ms: int = 900,
        no_speech_timeout_ms: int = 6000,
        max_utterance_ms: int = 30_000,
        pre_roll_ms: int = 300,
        min_speech_ms: int = 300,
        skip_ms: int = 320,
    ):
        self.vad = vad
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.no_speech_timeout_ms = no_speech_timeout_ms
        self.max_utterance_ms = max_utterance_ms
        self.min_speech_ms = min_speech_ms      # shorter blips (wake-word tail, a cough) are discarded
        self.skip_ms = skip_ms                  # ignore the wake word's tail + the chime right after detection
        self._pre_roll = deque(maxlen=max(1, pre_roll_ms * SAMPLE_RATE // 1000 // FRAME_SAMPLES))
        self.reset()

    def reset(self) -> None:
        self.vad.reset()
        self._frames: list[np.ndarray] = []
        self._speech_started = False
        self._elapsed_ms = 0
        self._silence_run_ms = 0
        self._speech_ms = 0
        self._pre_roll.clear()

    @property
    def speech_started(self) -> bool:
        return self._speech_started

    def feed(self, frame: np.ndarray) -> Utterance | None:
        """Return an Utterance when the user has finished, else None.

        Raises TimeoutError if no speech starts within no_speech_timeout_ms.
        """
        frame_ms = len(frame) * 1000 // SAMPLE_RATE
        self._elapsed_ms += frame_ms
        if self._elapsed_ms <= self.skip_ms:
            return None
        prob = self.vad.process(frame)
        is_speech = prob >= self.threshold

        if not self._speech_started:
            self._pre_roll.append(frame)
            if is_speech:
                self._speech_started = True
                self._frames.extend(self._pre_roll)
                self._silence_run_ms = 0
                log.debug("speech started at %d ms", self._elapsed_ms)
            elif self._elapsed_ms >= self.no_speech_timeout_ms:
                raise TimeoutError("no speech detected")
            return None

        self._frames.append(frame)
        if is_speech:
            self._speech_ms += frame_ms
        self._silence_run_ms = 0 if is_speech else self._silence_run_ms + frame_ms
        if self._silence_run_ms >= self.silence_ms or self._elapsed_ms >= self.max_utterance_ms:
            if self._speech_ms < self.min_speech_ms and self._elapsed_ms < self.max_utterance_ms:
                log.debug("blip of %d ms speech discarded; still listening", self._speech_ms)
                self._frames, self._speech_started, self._speech_ms, self._silence_run_ms = [], False, 0, 0
                self._pre_roll.clear()
                return None
            audio = np.concatenate(self._frames)
            return Utterance(audio=audio, duration_s=len(audio) / SAMPLE_RATE, speech_ms=self._speech_ms)
        return None
