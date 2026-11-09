"""Offline self-test of every pipeline stage using synthesized audio (no mic needed).

Usage: uv run scripts/selftest.py
"""

from __future__ import annotations

import logging
import sys
import time

import numpy as np

from smitty.audio.base import FRAME_SAMPLES, SAMPLE_RATE, resample, to_int16
from smitty.config import settings
from smitty.tts import KokoroTTS, MacSayTTS
from smitty.vad import EndpointDetector, SileroVAD

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("selftest")


def frames_of(pcm16: np.ndarray):
    pad = (-len(pcm16)) % FRAME_SAMPLES
    pcm16 = np.concatenate([pcm16, np.zeros(pad, dtype=np.int16)])
    return [pcm16[i : i + FRAME_SAMPLES] for i in range(0, len(pcm16), FRAME_SAMPLES)]


def say16k(text: str, tts) -> np.ndarray:
    pcm, sr = tts.synthesize_sync(text)
    return to_int16(resample(pcm, sr, SAMPLE_RATE))


def main() -> int:
    ok = True
    say = MacSayTTS()
    phrase = settings.wake_model.replace("_", " ")

    # --- Kokoro --------------------------------------------------------------
    print("\n[1/4] Kokoro TTS")
    t0 = time.monotonic()
    kokoro = KokoroTTS(settings.kokoro_model_path, settings.kokoro_voices_path, voice=settings.kokoro_voice)
    pcm, sr = kokoro.synthesize_sync(phrase.capitalize() + ".")
    print(f"      loaded in {time.monotonic()-t0:.1f}s; {phrase!r} -> {len(pcm)/sr:.2f}s @ {sr} Hz, peak {np.abs(pcm).max():.2f}")
    ok &= sr > 0 and len(pcm) > sr * 0.3

    # --- Wake word -----------------------------------------------------------
    print("\n[2/4] openWakeWord")
    from smitty.wake import WakeWordDetector
    wake = WakeWordDetector(settings.wake_model, settings.wake_threshold)
    for label, audio in [
        ("silence", np.zeros(SAMPLE_RATE * 2, dtype=np.int16)),
        (f"kokoro {phrase!r}", np.concatenate([np.zeros(SAMPLE_RATE // 2, np.int16), to_int16(resample(pcm, sr, SAMPLE_RATE)), np.zeros(SAMPLE_RATE // 2, np.int16)])),
        (f"say {phrase!r}", np.concatenate([np.zeros(SAMPLE_RATE // 2, np.int16), say16k(phrase, say), np.zeros(SAMPLE_RATE // 2, np.int16)])),
        ("say 'what time is it'", np.concatenate([np.zeros(SAMPLE_RATE // 2, np.int16), say16k("what time is it", say), np.zeros(SAMPLE_RATE // 2, np.int16)])),
    ]:
        wake.reset()
        best = max(wake.process(f) for f in frames_of(audio))
        expect = phrase in label
        hit = best >= settings.wake_threshold
        print(f"      {label:<24} max score {best:.3f}  {'HIT' if hit else '---'}  {'ok' if hit == expect else 'UNEXPECTED'}")
        ok &= hit == expect

    # --- VAD + endpointing ---------------------------------------------------
    print("\n[3/4] Silero VAD + endpoint detector")
    vad = SileroVAD(settings.vad_model_path)
    speech = say16k("Open the smitty project and run the tests.", say)
    sil = np.zeros(SAMPLE_RATE, dtype=np.int16)
    p_sil = max(vad.process(f) for f in frames_of(sil))
    vad.reset()
    p_sp = max(vad.process(f) for f in frames_of(speech))
    print(f"      silence prob {p_sil:.3f}, speech prob {p_sp:.3f}")
    ok &= p_sil < 0.3 < p_sp

    ep = EndpointDetector(vad, threshold=settings.vad_threshold, silence_ms=settings.vad_silence_ms)
    stream = np.concatenate([np.zeros(SAMPLE_RATE // 2, np.int16), speech, np.zeros(SAMPLE_RATE * 2, np.int16)])
    utt = None
    for i, f in enumerate(frames_of(stream)):
        utt = ep.feed(f)
        if utt is not None:
            print(f"      endpoint fired after {(i+1)*80} ms; utterance {utt.duration_s:.2f}s (speech was {len(speech)/SAMPLE_RATE:.2f}s)")
            break
    ok &= utt is not None

    # --- Whisper -------------------------------------------------------------
    print("\n[4/4] mlx-whisper")
    from smitty.stt import WhisperSTT
    stt = WhisperSTT(settings.whisper_model, settings.whisper_language)
    t0 = time.monotonic(); stt.warm_up(); print(f"      warm-up {time.monotonic()-t0:.1f}s")
    t0 = time.monotonic(); text = stt.transcribe_sync(utt.audio if utt is not None else speech)
    print(f"      transcript ({time.monotonic()-t0:.2f}s): {text!r}")
    ok &= "smitty" in text.lower() or "tests" in text.lower()

    print("\nRESULT:", "ALL OK" if ok else "SOMETHING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
