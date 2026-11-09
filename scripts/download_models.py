"""Fetch all model weights into ./models (and the HF cache for whisper).

Usage: uv run scripts/download_models.py [--skip-whisper]
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

from smitty.config import MODELS_DIR, settings

SILERO_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"
KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def fetch(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  ok      {dest.name} ({dest.stat().st_size // 1_000_000} MB, cached)")
        return
    print(f"  fetch   {dest.name} <- {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def hook(blocks, bs, total):
        if total > 0:
            pct = min(100, blocks * bs * 100 // total)
            print(f"\r          {pct:3d}%", end="", flush=True)

    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    tmp.rename(dest)
    print(f"\r  done    {dest.name} ({dest.stat().st_size // 1_000_000} MB)")


def main(skip_whisper: bool) -> int:
    MODELS_DIR.mkdir(exist_ok=True)

    print("openWakeWord:")
    import openwakeword
    res = Path(openwakeword.__file__).parent / "resources" / "models"
    if hasattr(openwakeword, "utils") and hasattr(openwakeword.utils, "download_models"):
        openwakeword.utils.download_models(model_names=[settings.wake_model])
    found = sorted(p.name for p in res.glob(f"{settings.wake_model}*.onnx"))
    print(f"  ok      bundled: {found}" if found else f"  MISSING {settings.wake_model} in {res}")

    print("Silero VAD:")
    fetch(SILERO_URL, settings.vad_model_path)

    print("Kokoro:")
    fetch(KOKORO_BASE + "kokoro-v1.0.onnx", settings.kokoro_model_path)
    fetch(KOKORO_BASE + "voices-v1.0.bin", settings.kokoro_voices_path)

    if not skip_whisper:
        print(f"Whisper ({settings.whisper_model}):")
        from huggingface_hub import snapshot_download
        path = snapshot_download(settings.whisper_model)
        print(f"  ok      {path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-whisper", action="store_true")
    sys.exit(main(ap.parse_args().skip_whisper))
