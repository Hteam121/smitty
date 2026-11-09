"""Query the XVF3800 through the XIAO (WebSocket -> I2C). Remote console for diagnostics.

Usage: uv run scripts/xvf_query.py            # standard report
       uv run scripts/xvf_query.py --watch 10  # repeat the I2S lines every second for 10 s
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys

from smitty.audio.websocket import WebSocketAudio
from smitty.config import settings

# name: (resid, cmd, count, type)
PARAMS = {
    "VERSION": (48, 0, 3, "u8"), "BLD_MSG": (48, 1, 50, "char"),
    "I2S_INACTIVE": (35, 24, 1, "u8"), "I2S_CURRENT_IDLE_TIME": (35, 7, 1, "i32"), "I2S_MIN_IDLE_TIME": (35, 8, 1, "i32"),
    "I2S_INPUT_PACKED": (35, 10, 1, "u8"), "I2S_DAC_DSP_ENABLE": (35, 27, 1, "u8"),
    "AUDIO_MGR_OP_L": (35, 15, 2, "u8"), "AUDIO_MGR_OP_R": (35, 19, 2, "u8"),
    "AUDIO_MGR_MIC_GAIN": (35, 0, 1, "f32"), "AUDIO_MGR_REF_GAIN": (35, 1, 1, "f32"),
    "GPO_READ_VALUES": (20, 0, 5, "u8"), "LED_EFFECT": (20, 12, 1, "u8"), "DOA_VALUE": (20, 18, 2, "u16"),
}
SIZE = {"u8": 1, "char": 1, "u16": 2, "i32": 4, "f32": 4}
FMT = {"u8": "B", "u16": "H", "i32": "i", "f32": "f"}


async def read(link: WebSocketAudio, name: str):
    resid, cmd, count, kind = PARAMS[name]
    raw = await link.xmos_read(resid, cmd, count * SIZE[kind])
    if raw is None:
        return None
    if kind == "char":
        return raw.split(b"\0")[0].decode("ascii", "replace")
    return list(struct.unpack("<" + FMT[kind] * count, raw))


async def main(watch: int) -> int:
    logging.basicConfig(level=logging.WARNING)
    link = WebSocketAudio(settings.ws_host, settings.ws_port)
    await link.start()
    print(f"waiting for the XIAO on port {settings.ws_port}...")
    if not await link.wait_connected(60):
        print("no device connected"); return 1
    for _ in range(50):                       # wait for the board's hello so we know its loop is live
        if "fw" in link._device:
            break
        await asyncio.sleep(0.1)
    print(f"device: {link._device}")
    for name in PARAMS:
        print(f"  {name:<24} {await read(link, name)}")
    for i in range(watch):
        await asyncio.sleep(1)
        print(f"  t+{i+1:>2}s  I2S_INACTIVE={await read(link, 'I2S_INACTIVE')}  CUR_IDLE={await read(link, 'I2S_CURRENT_IDLE_TIME')}  "
              f"MIN_IDLE={await read(link, 'I2S_MIN_IDLE_TIME')}  DOA={await read(link, 'DOA_VALUE')}  stat={ {k: link.device_stat.get(k) for k in ('i2s_frames', 'raw_l', 'raw_r')} }")
    await link.stop()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0)
    sys.exit(asyncio.run(main(ap.parse_args().watch)))
