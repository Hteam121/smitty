"""Wi-Fi implementation of AudioSource / AudioSink: a WebSocket server the
XIAO ESP32S3 (soldered to the reSpeaker XVF3800) connects to.

Protocol (ws://<mac>:8766/audio), deliberately tiny so the firmware stays small:
  device -> server   binary: int16 LE mono PCM at 16 kHz, any frame size (the
                     firmware sends 20 ms = 640 bytes); text: JSON control
                     {"type":"hello","device":"xiao-xvf3800","sr":16000}, {"type":"pong"}
  server -> device   binary: int16 LE mono PCM at 16 kHz to play, in 20 ms packets,
                     paced slightly ahead of real time so the device needs only a
                     small ring buffer; text: {"type":"stop"} = flush playback now,
                     {"type":"ping"}

One device at a time; a new connection replaces the old one. While no device is
connected the source simply yields nothing and the sink drops audio (logged).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import numpy as np
from aiohttp import WSMsgType, web

from smitty import store
from smitty.audio.base import FRAME_SAMPLES, SAMPLE_RATE, AudioSink, AudioSource, resample, to_float32, to_int16

log = logging.getLogger(__name__)

PACKET_SAMPLES = 320            # 20 ms at 16 kHz
HELLO_TIMEOUT_S = 3.0           # a device that connects but never says hello gets dropped (it reconnects in ~2 s)
LEAD_S = 0.25                   # how far ahead of real time playback packets are pushed


class WebSocketAudio:
    """Owns the server and the single device connection; exposes a source and a sink."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8766, path: str = "/audio", queue_frames: int = 64):
        self.host, self.port, self.path = host, port, path
        self._ws: web.WebSocketResponse | None = None
        self._device: dict = {}
        self._runner: web.AppRunner | None = None
        self._in_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_frames)
        self._carry = np.zeros(0, dtype=np.int16)
        self._dropped = 0
        self._connected = asyncio.Event()
        self.device_stat: dict = {}
        self._xmos_pending: dict[int, asyncio.Future] = {}
        self._xmos_id = 0
        self._hello = asyncio.Event()
        self.on_ready: list = []        # callables invoked (no args) each time a device has said hello
        self.source = _WsSource(self)
        self.sink = _WsSink(self)
        self.stats = {"rx_packets": 0, "rx_bytes": 0, "tx_packets": 0, "tx_bytes": 0, "connected_at": None}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._runner:
            return
        app = web.Application()
        app.add_routes([web.get(self.path, self._handle), web.get("/", self._index)])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("WebSocket audio server on ws://%s:%d%s", self.host, self.port, self.path)

    async def stop(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------ handlers
    async def _index(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "connected": self.connected, "device": self._device, **self.stats})

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10.0, max_msg_size=1 << 20)
        await ws.prepare(request)
        if self._ws is not None and not self._ws.closed:
            log.warning("new device connection from %s replaces the previous one", request.remote)
            await self._ws.close()
        self._ws = ws
        self._device = {"remote": request.remote}
        self.stats["connected_at"] = time.time()
        self._hello.clear()
        log.info("audio device connected from %s", request.remote)

        async def hello_watchdog():
            try:
                await asyncio.wait_for(self._hello.wait(), HELLO_TIMEOUT_S)
            except asyncio.TimeoutError:
                if self._ws is ws and not ws.closed:
                    log.warning("device at %s connected but sent no hello in %.0fs; dropping so it reconnects",
                                request.remote, HELLO_TIMEOUT_S)
                    store.record("device", "Wi-Fi device stalled; reconnecting", remote=request.remote)
                    await ws.close()
        watchdog = asyncio.create_task(hello_watchdog())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._on_pcm(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    self._on_text(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            watchdog.cancel()
            if self._ws is ws:
                self._ws = None
                self._connected.clear()
                self._hello.clear()
                self.sink.interrupt()
            log.info("audio device disconnected (%s)", request.remote)
            store.record("device", "Wi-Fi device disconnected", remote=request.remote)
            store.put("device", {"connected": False})
        return ws

    def _on_text(self, data: str) -> None:
        try:
            m = json.loads(data)
        except json.JSONDecodeError:
            return
        if m.get("type") == "xmos":
            fut = self._xmos_pending.pop(m.get("id"), None)
            if fut and not fut.done():
                fut.set_result(bytes(m.get("data", [])) if m.get("ok") else None)
            return
        if m.get("type") == "presence":
            log.info("device presence: codec=%s xvf3800=%s (t=%ss)", m.get("codec"), m.get("xvf3800"), m.get("t"))
            return
        if m.get("type") == "stat":
            self.device_stat = {k: v for k, v in m.items() if k != "type"}
            log.info("device stat: %s", self.device_stat)
            store.put("device", {"connected": True, **self._device, **self.device_stat})
            return
        if m.get("type") == "hello":
            self._device.update({k: v for k, v in m.items() if k != "type"})
            log.info("device hello: %s", self._device)
            store.record("device", "Wi-Fi device connected", **self._device)
            store.put("device", {"connected": True, **self._device})
            self._hello.set()
            self._connected.set()           # "connected" means: handshake done AND the device is talking
            for cb in list(self.on_ready):
                try:
                    cb()
                except Exception:
                    log.exception("on_ready callback failed")
            if int(m.get("sr", SAMPLE_RATE)) != SAMPLE_RATE:
                log.warning("device sample rate %s != %d; audio will be wrong", m.get("sr"), SAMPLE_RATE)

    def _on_pcm(self, data: bytes) -> None:
        self.stats["rx_packets"] += 1
        self.stats["rx_bytes"] += len(data)
        pcm = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2")
        buf = np.concatenate([self._carry, pcm]) if len(self._carry) else pcm
        n = len(buf) // FRAME_SAMPLES * FRAME_SAMPLES
        for i in range(0, n, FRAME_SAMPLES):
            frame = np.ascontiguousarray(buf[i:i + FRAME_SAMPLES])
            if self._in_q.full():
                self._in_q.get_nowait()
                self._dropped += 1
            self._in_q.put_nowait(frame)
        self._carry = buf[n:].copy()

    # ---- XVF3800 control through the XIAO's I2C (same resource/command ids as the USB tool)
    async def xmos_read(self, resid: int, cmd: int, n: int, timeout: float = 2.0) -> bytes | None:
        """Read n bytes of a control parameter; None if the device or the XVF3800 didn't answer."""
        if not self.connected:
            return None
        self._xmos_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._xmos_pending[self._xmos_id] = fut
        await self.send_text({"type": "xmos_read", "id": self._xmos_id, "resid": resid, "cmd": cmd, "n": n})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._xmos_pending.pop(self._xmos_id, None)
            return None

    async def xmos_write(self, resid: int, cmd: int, data: bytes, timeout: float = 2.0) -> bool:
        if not self.connected:
            return False
        self._xmos_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._xmos_pending[self._xmos_id] = fut
        await self.send_text({"type": "xmos_write", "id": self._xmos_id, "resid": resid, "cmd": cmd, "data": list(data)})
        try:
            return (await asyncio.wait_for(fut, timeout)) is not None
        except asyncio.TimeoutError:
            self._xmos_pending.pop(self._xmos_id, None)
            return False

    async def send_text(self, obj: dict) -> None:
        if self.connected:
            await self._ws.send_str(json.dumps(obj))

    async def send_pcm(self, packet: np.ndarray) -> None:
        if self.connected:
            data = to_int16(packet).astype("<i2").tobytes()
            await self._ws.send_bytes(data)
            self.stats["tx_packets"] += 1
            self.stats["tx_bytes"] += len(data)


class _WsSource(AudioSource):
    def __init__(self, link: WebSocketAudio):
        self._link = link
        self._running = False

    async def start(self) -> None:
        await self._link.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def frames(self) -> AsyncIterator[np.ndarray]:
        while self._running:
            yield await self._link._in_q.get()

    @property
    def backlog(self) -> int:
        return self._link._in_q.qsize()

    def drain(self) -> int:
        n = 0
        while not self._link._in_q.empty():
            self._link._in_q.get_nowait()
            n += 1
        return n


class _WsSink(AudioSink):
    def __init__(self, link: WebSocketAudio, volume: float = 1.0):
        self._link = link
        self.volume = volume
        self._playing = False
        self._interrupted = asyncio.Event()

    async def start(self) -> None:
        await self._link.start()

    async def stop(self) -> None:
        self.interrupt()

    @property
    def is_playing(self) -> bool:
        return self._playing

    def interrupt(self) -> None:
        self._interrupted.set()          # play() notices at its next packet and returns
        self._playing = False
        if self._link.connected:
            asyncio.ensure_future(self._link.send_text({"type": "stop"}))

    async def play(self, pcm: np.ndarray, sample_rate: int) -> None:
        if not self._link.connected:
            log.warning("no audio device connected; dropping %.1fs of playback", len(pcm) / sample_rate)
            return
        audio = resample(to_float32(np.asarray(pcm).reshape(-1)), sample_rate, SAMPLE_RATE) * self.volume
        self._interrupted.clear()
        self._playing = True
        try:
            t0 = time.monotonic()
            for i in range(0, len(audio), PACKET_SAMPLES):
                if self._interrupted.is_set() or not self._link.connected:
                    log.info("playback cut short (%s)", "interrupted" if self._interrupted.is_set() else "device gone")
                    return
                packet = audio[i:i + PACKET_SAMPLES]
                await self._link.send_pcm(packet)
                # pace: stay LEAD_S ahead of real time, never further
                ahead = (i + len(packet)) / SAMPLE_RATE - (time.monotonic() - t0)
                if ahead > LEAD_S:
                    await asyncio.sleep(ahead - LEAD_S)
            # let the device finish what it buffered
            await asyncio.sleep(max(0.0, len(audio) / SAMPLE_RATE - (time.monotonic() - t0)))
        finally:
            self._playing = False
