"""Pretend to be the XIAO ESP32S3: stream a WAV (or synthesized speech) as the mic and
record whatever the server plays into a WAV. Lets the WebSocket transport be tested
without hardware, and documents the protocol the firmware implements.

Usage: uv run scripts/ws_fake_device.py [--url ws://127.0.0.1:8766/audio] [--say "hey smitty what time is it"] [--out received.wav]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave

import aiohttp
import numpy as np

from smitty.audio.base import SAMPLE_RATE, resample, to_int16
from smitty.tts import MacSayTTS

PACKET = 320  # 20 ms


async def main(url: str, say: str, out: str, seconds: float) -> int:
    pcm, sr = MacSayTTS().synthesize_sync(say)
    mic = to_int16(resample(pcm, sr, SAMPLE_RATE))
    mic = np.concatenate([np.zeros(SAMPLE_RATE, np.int16), mic, np.zeros(int(seconds * SAMPLE_RATE), np.int16)])
    received: list[bytes] = []
    stops = 0
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            await ws.send_str(json.dumps({"type": "hello", "device": "fake-device", "sr": SAMPLE_RATE, "fw": "py"}))
            print(f"connected to {url}; streaming {len(mic)/SAMPLE_RATE:.1f}s of mic audio ({say!r})")

            async def sender():
                t0 = time.monotonic()
                try:
                    for i in range(0, len(mic), PACKET):
                        if ws.closed:
                            return
                        await ws.send_bytes(mic[i:i + PACKET].astype("<i2").tobytes())
                        await asyncio.sleep(max(0.0, (i + PACKET) / SAMPLE_RATE - (time.monotonic() - t0)))
                    await ws.close()
                except (aiohttp.ClientConnectionError, ConnectionResetError):
                    print("server closed the connection first (normal when the test finishes)")

            send_task = asyncio.create_task(sender())
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    received.append(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("type") == "stop":
                        stops += 1
                    elif m.get("type") == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
            await send_task
    audio = np.frombuffer(b"".join(received), dtype="<i2")
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(audio.tobytes())
    peak = int(np.abs(audio).max()) if len(audio) else 0
    print(f"received {len(received)} packets = {len(audio)/SAMPLE_RATE:.2f}s of playback (peak {peak}), {stops} stop(s); wrote {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8766/audio")
    ap.add_argument("--say", default="hey smitty, what time is it")
    ap.add_argument("--out", default="received.wav")
    ap.add_argument("--seconds", type=float, default=12.0, help="silence to keep streaming after the phrase")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url, a.say, a.out, a.seconds)))
