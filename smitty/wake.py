"""Wake word detection with openWakeWord ("hey_jarvis" by default)."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, model_name: str = "hey_smitty", threshold: float = 0.5, refractory_s: float = 2.0, patience: int = 2):
        import warnings

        import openwakeword
        from openwakeword.model import Model  # imported lazily: heavy

        # openWakeWord asks onnxruntime for CUDA; harmless on a Mac, silence it.
        warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")

        self.model_name = model_name
        self.threshold = threshold
        self.refractory_s = refractory_s
        self.patience = patience  # consecutive 80 ms frames that must exceed the threshold
        self._hot = 0
        self._frames_since_hit = 10**9  # frame-based refractory: deterministic, not wall-clock
        self._refractory_frames = int(refractory_s * 1000 / 80)
        path = Path(model_name)
        if not path.exists():  # a bare name: ./models/<name>.onnx (custom) or openWakeWord's bundled ones
            from smitty.config import MODELS_DIR
            res = Path(openwakeword.__file__).parent / "resources" / "models"
            exact = MODELS_DIR / f"{model_name}.onnx"
            if exact.exists():
                path = exact  # never pick a sibling like <name>.prev.onnx by glob order
            else:
                candidates = sorted(res.glob(f"{model_name}*.onnx"))
                if not candidates:
                    raise FileNotFoundError(
                        f"no wake word model matching {model_name!r} in {MODELS_DIR} or {res}; "
                        f"train one with: uv run scripts/train_wakeword.py --phrase '{model_name.replace('_', ' ')}'"
                    )
                path = candidates[-1]
        log.info("wake word model file: %s", path)
        self._model = Model(wakeword_model_paths=[str(path)])
        # The key openWakeWord uses for scores is the model file's stem (e.g. "hey_smitty", "hey_jarvis_v0.1").
        self._key = next(iter(self._model.models.keys()))
        log.info("wake word model loaded: %s", self._key)

    def process(self, frame: np.ndarray) -> float:
        """Feed one 80 ms int16 frame; return the current score in [0, 1]."""
        scores = self._model.predict(frame)
        return float(scores[self._key])

    def detect(self, frame: np.ndarray) -> bool:
        score = self.process(frame)
        self.last_score = score
        self._hot = self._hot + 1 if score >= self.threshold else 0
        self._frames_since_hit += 1
        if self._hot >= self.patience and self._frames_since_hit > self._refractory_frames:
            self._frames_since_hit = 0
            self._hot = 0
            log.info("wake word %r detected (score %.2f)", self.model_name, score)
            return True
        return False

    def reset(self) -> None:
        self._hot = 0
        self._model.reset()
