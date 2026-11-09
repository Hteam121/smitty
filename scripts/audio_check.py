"""Step 1 sanity check: is the reSpeaker there, can we record and play through it?

Usage:  uv run scripts/audio_check.py [--seconds 3] [--no-play]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import numpy as np

from smitty.audio import FRAME_SAMPLES, SAMPLE_RATE
from smitty.audio.usb import UsbAudioSink, UsbAudioSource, describe_devices, find_device


async def main(seconds: float, play: bool) -> int:
    print(describe_devices())
    idx = find_device()
    if idx is None:
        print("\nFAIL: reSpeaker XVF3800 not found in sounddevice list.")
        return 1
    print(f"\nreSpeaker found at index {idx}\n")

    # --- Record -------------------------------------------------------------
    n_frames = int(seconds * SAMPLE_RATE / FRAME_SAMPLES)
    captured: list[np.ndarray] = []
    print(f"Recording {seconds:.1f}s... say something.")
    t0 = time.monotonic()
    async with UsbAudioSource() as src:
        async for frame in src.frames():
            captured.append(frame)
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            bar = "#" * min(40, int(rms / 100))
            print(f"\r  level {rms:7.0f} |{bar:<40}|", end="", flush=True)
            if len(captured) >= n_frames:
                break
    elapsed = time.monotonic() - t0
    audio = np.concatenate(captured)
    peak = int(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    print(f"\n  captured {len(audio)} samples in {elapsed:.2f}s  peak={peak}  rms={rms:.0f}")
    if peak < 50:
        print("  WARN: signal looks silent. Check macOS mic permission for the terminal app.")

    # --- Play back ----------------------------------------------------------
    if play:
        print("Playing a 440 Hz beep, then your recording...")
        t = np.arange(int(0.3 * SAMPLE_RATE)) / SAMPLE_RATE
        beep = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        async with UsbAudioSink() as sink:
            await sink.play(beep, SAMPLE_RATE)
            await asyncio.sleep(0.2)
            await sink.play(audio, SAMPLE_RATE)
            await asyncio.sleep(0.1)
        print("Playback done.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--no-play", action="store_true")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO)
    sys.exit(asyncio.run(main(args.seconds, not args.no_play)))
