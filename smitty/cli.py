"""Command line entry point: `uv run smitty <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from smitty.config import settings

log = logging.getLogger(__name__)

API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _apply_auth_policy() -> None:
    """Decide which credential the bundled Claude Code CLI (brain + workers) will see.

    The CLI prefers an API key over the claude.ai login whenever one is in the
    environment. The user's shell exports one, so by default we drop it from our
    own process environment (which the CLI inherits) and let the login win.
    """
    present = [v for v in API_KEY_VARS if os.environ.get(v)]
    if settings.auth == "api_key":
        log.info("auth: API key (%s)", ", ".join(present) or "none set!")
        return
    for v in present:
        os.environ.pop(v, None)
    log.info("auth: claude.ai login%s", f" (ignored {', '.join(present)})" if present else "")


def _make_audio():
    if settings.audio_transport == "usb":
        from smitty.audio.usb import UsbAudioSink, UsbAudioSource

        return UsbAudioSource(), UsbAudioSink(volume=settings.volume)
    if settings.audio_transport in ("websocket", "ws", "wifi"):
        from smitty.audio.websocket import WebSocketAudio

        link = WebSocketAudio(settings.ws_host, settings.ws_port)
        link.sink.volume = settings.volume
        return link.source, link.sink
    raise ValueError(f"unknown audio transport {settings.audio_transport!r}")


async def cmd_ws_test(seconds: float) -> None:
    """Wait for the ESP32 (or the fake device) to connect, play a beep + a sentence to it,
    record from it for a few seconds and report levels. Proves Wi-Fi, speaker and mic."""
    import socket
    import numpy as np
    from smitty.audio.base import FRAME_SAMPLES, SAMPLE_RATE
    from smitty.audio.websocket import WebSocketAudio
    from smitty.tts import make_tts

    link = WebSocketAudio(settings.ws_host, settings.ws_port)
    link.sink.volume = settings.volume
    await link.start()
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "?"
    print(f"waiting for a device on ws://{ip}:{settings.ws_port}/audio  (Ctrl-C to stop)")
    if not await link.wait_connected(timeout=600):
        print("no device connected"); return
    print(f"device: {link._device}")
    await asyncio.sleep(0.5)
    t = np.arange(int(0.3 * SAMPLE_RATE)) / SAMPLE_RATE
    beep = (0.4 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    print("playing beep...")
    await link.sink.play(beep, SAMPLE_RATE)
    tts = make_tts(settings)
    pcm, sr = await tts.synthesize("Smitty speaker test. If you can hear this, the speaker works.")
    print(f"playing {len(pcm)/sr:.1f}s of speech...")
    await link.sink.play(pcm, sr)
    print(f"recording {seconds:.0f}s from the device mic... say something")
    await link.source.start()
    got = []
    n_frames = int(seconds * SAMPLE_RATE / FRAME_SAMPLES)
    try:
        async with asyncio.timeout(seconds + 5):
            async for frame in link.source.frames():
                got.append(frame)
                rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                print(f"\r  level {rms:7.0f} |{'#' * min(40, int(rms / 100)):<40}|", end="", flush=True)
                if len(got) >= n_frames:
                    break
    except TimeoutError:
        print("\n  (timed out waiting for mic frames)")
    audio = np.concatenate(got) if got else np.zeros(0, np.int16)
    peak = int(np.abs(audio).max()) if len(audio) else 0
    print(f"\nreceived {len(audio)/SAMPLE_RATE:.2f}s, peak {peak}; stats {link.stats}")
    import wave
    with wave.open("ws_test_received.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(audio.tobytes())
    print("wrote ws_test_received.wav")
    await link.stop()


_lock_file = None


def _single_instance() -> bool:
    """Only one Smitty (voice, chat or text-only) may run per data folder: they share ports, schedules and sessions."""
    import fcntl
    global _lock_file
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _lock_file = open(settings.data_dir / "smitty.lock", "a+")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_file.seek(0)
        print(f"Smitty is already running ({_lock_file.read().strip() or 'another process'}). Stop that one first.")
        return False
    _lock_file.seek(0)
    _lock_file.truncate()
    _lock_file.write(f"pid {os.getpid()}")
    _lock_file.flush()
    return True


def _make_attention():
    from smitty.attention import Attention
    return Attention(approval_wait_s=settings.approval_wait_s, default_mode=settings.attention,
                     artifacts_dir=settings.data_dir / "artifacts", review_wait_s=settings.review_wait_s)


def _make_control(server, brain, attention, mode: str, base_state=lambda: "idle"):
    """Let the dashboard send chat, approvals and schedule/session actions to this process."""
    from smitty.control import Control
    control = Control(brain, attention, settings.data_dir, mode, base_state=base_state, volume=getattr(brain, "volume", None))
    control.attach(server.app)
    return control


def _make_claude_brain(confirm, notify, attention=None):
    from smitty.brain.agent import ClaudeBrain
    from smitty.volume import Volume
    from smitty.brain.sessions import SessionManager
    from smitty.scheduler import Scheduler
    from smitty.weather import Weather

    from smitty.vault import open_vault

    settings.tasks_dir.mkdir(parents=True, exist_ok=True)
    vault = open_vault(settings.vault_dir)
    # With the attention hub an approval may wait in the dashboard, so the session waits that long too.
    wait_s = settings.approval_wait_s + 30 if attention is not None else settings.confirm_timeout_s
    mgr = SessionManager(settings.project_roots, confirm=confirm, notify=notify,
                         worker_model=settings.worker_model, confirm_timeout_s=wait_s,
                         tasks_dir=settings.tasks_dir, review=getattr(attention, "review", None),
                         vault_dir=vault.root if vault else None, browser_profile=settings.browser_profile)
    from smitty.brain.notices import NoticeBoard
    mgr.notices = NoticeBoard(settings.data_dir / "missed.json", settings.quiet_hours)
    scheduler = Scheduler(mgr, settings.tasks_dir, settings.data_dir / "schedules.json",
                          min_interval_min=settings.schedule_min_interval_min)
    weather = Weather(settings.data_dir / "state.json", units=settings.weather_units)
    volume = Volume(settings)        # loads the saved level before any speaker is opened
    brain = ClaudeBrain(mgr, model=settings.brain_model, effort=settings.brain_effort,
                        assistant_name=settings.assistant_name, user_name=settings.user_name,
                        cwd=str(settings.tasks_dir), scheduler=scheduler, weather=weather, attention=attention,
                        volume=volume, vault=vault)
    brain.volume = volume
    # Models picked in the dashboard are remembered; without a pick the configured ones apply.
    from smitty import store
    prefs = store.get("model_prefs") or {}
    if prefs.get("default"):
        mgr.worker_model = str(prefs["default"])
    if prefs.get("brain"):
        brain.use_model(str(prefs["brain"]))
    return brain


async def cmd_say(text: str) -> None:
    from smitty.tts import make_tts

    tts = make_tts(settings)
    pcm, sr = await tts.synthesize(text)
    _, sink = _make_audio()
    async with sink:
        await sink.play(pcm, sr)
        await asyncio.sleep(0.1)


def _systems(mode: str, brain, tts=None, leds=None, note: str = "") -> list[dict]:
    """A plain-language list of Smitty's parts and whether each is in working order."""
    out = []
    if mode == "voice":
        wifi = settings.audio_transport in ("websocket", "ws", "wifi")
        out.append({"name": "Microphone and speaker", "ok": True,
                    "detail": "XIAO ESP32S3 over Wi-Fi" if wifi else "reSpeaker XVF3800 over USB"})
        out.append({"name": "Wake word", "ok": True,
                    "detail": f"{settings.wake_model.replace('_', ' ')}, threshold {settings.wake_threshold}, {settings.wake_verify} check"})
        out.append({"name": "Speech to text", "ok": True, "detail": settings.whisper_model.split("/")[-1] + " (local)"})
        if tts is not None:
            kokoro = type(tts).__name__ == "KokoroTTS"
            out.append({"name": "Voice", "ok": True, "detail": f"Kokoro {settings.kokoro_voice} (local)" if kokoro else "macOS say (fallback)"})
        out.append({"name": "LED ring", "ok": leds is not None, "detail": ("through the ESP32" if wifi else "USB control") if leds else "off"})
    else:
        out.append({"name": "Mode", "ok": True, "detail": note or ("text only, no microphone or speaker: chat from the dashboard"
                    if mode == "text" else "text chat in the terminal, no audio")})
    if hasattr(brain, "mgr"):
        from smitty.brain.models import label
        out.append({"name": "Brain", "ok": True, "detail": f"Claude {label(brain.model)}, {'claude.ai login' if settings.auth != 'api_key' else 'API key'}"})
        n = sum(1 for x in brain.scheduler.schedules.values() if x.enabled) if getattr(brain, "scheduler", None) else 0
        out.append({"name": "Scheduler", "ok": True, "detail": f"{n} active schedule{'' if n == 1 else 's'}" + (f", quiet hours {settings.quiet_hours}" if settings.quiet_hours else "")})
        if settings.vault_dir:
            v = getattr(brain, "vault", None)
            out.append({"name": "Notes", "ok": v is not None,
                        "detail": f"Obsidian vault {v.root.name}" if v else f"vault folder not found: {settings.vault_dir}"})
    else:
        out.append({"name": "Brain", "ok": True, "detail": "echo (no Claude)"})
    out.append({"name": "Claude Code hooks", "ok": True, "detail": f"listening on port {settings.http_port}"})
    return out


async def _heartbeat(mode, brain, started: float, control: bool = False, systems=None, extra=None) -> None:
    """Keep the store's status row fresh so the dashboard knows Smitty is alive."""
    import os
    from smitty import __version__, store
    while True:
        store.put("status", {**(extra() if extra else {}), "mode": mode() if callable(mode) else mode, "pid": os.getpid(), "started_at": started, "transport": settings.audio_transport,
                             "wake_model": settings.wake_model, "wake_threshold": settings.wake_threshold,
                             "volume": settings.volume, "brain_model": getattr(brain, "model", None) or settings.brain_model, "auth": settings.auth,
                             "quiet_hours": settings.quiet_hours, "version": __version__, "hooks_port": settings.http_port,
                             "control": control})
        if hasattr(brain, "mgr"):
            store.put("sessions", brain.mgr.describe_all())
            store.put("models", brain.models_snapshot())
        if systems is not None:
            store.put("systems", systems())      # the schedule count and similar details change while running
        await asyncio.sleep(4)


async def _start_dashboard(demo: bool = False):
    from smitty.dashboard.server import Dashboard
    dash = Dashboard(settings.data_dir / "smitty.db", settings.data_dir, port=settings.dashboard_port, demo=demo,
                     control_port=settings.http_port, tasks_dir=settings.tasks_dir)
    return dash if await dash.start() else None


def _start_hub(mode: str):
    """Mirror activity to the cloud hub and take the phone's commands, when a hub is configured."""
    from smitty.hub import Hub, load_config
    cfg = load_config(settings.data_dir)
    if cfg is None:
        log.info("hub: not configured (set SMITTY_HUB_URL/KEY or ~/.smitty/hub.json for the phone app)")
        return None
    hub = Hub(cfg[0], cfg[1], settings.data_dir / "smitty.db", settings.data_dir, settings.http_port,
              interval_s=settings.hub_interval_s, mode=mode)
    hub.start()
    return hub


async def cmd_hub(check: bool) -> None:
    """Run the hub sync on its own (Smitty may or may not be running), or test the connection once."""
    from smitty.hub import Hub, HubUnreachable, load_config
    cfg = load_config(settings.data_dir)
    if cfg is None:
        print("No hub configured. Set SMITTY_HUB_URL and SMITTY_HUB_KEY (service role key), or write ~/.smitty/hub.json "
              '{"url": "https://<project>.supabase.co", "key": "..."}')
        return
    hub = Hub(cfg[0], cfg[1], settings.data_dir / "smitty.db", settings.data_dir, settings.http_port,
              interval_s=settings.hub_interval_s, mode="standalone")
    if check:
        hub._load_cursor()
        try:
            await hub.sync_once()
            print(f"hub OK: {cfg[0]}  pushed {hub.stats['pushed']} row(s), ran {hub.stats['commands']} command(s)")
        except HubUnreachable as e:
            print(f"hub unreachable: {e}")
        finally:
            await hub.stop()
        return
    hub.start()
    print(f"hub sync running against {cfg[0]} (Ctrl-C to stop)")
    try:
        await asyncio.Event().wait()
    finally:
        await hub.stop()


async def cmd_dashboard(demo: bool) -> None:
    """Serve the dashboard on its own (Smitty does not need to be running)."""
    from smitty.dashboard.server import Dashboard, keep_demo_alive, seed_demo
    if demo:
        demo_dir = settings.data_dir / "demo"
        demo_dir.mkdir(parents=True, exist_ok=True)
        seed_demo(demo_dir / "smitty.db")
        dash = Dashboard(demo_dir / "smitty.db", demo_dir, port=settings.dashboard_port, demo=True)
        alive = asyncio.create_task(keep_demo_alive(demo_dir / "smitty.db"))
    else:
        dash = Dashboard(settings.data_dir / "smitty.db", settings.data_dir, port=settings.dashboard_port,
                         control_port=settings.http_port, tasks_dir=settings.tasks_dir)
    if not await dash.start():
        print(f"port {settings.dashboard_port} is busy; is the dashboard already running?")
        return
    print(f"Smitty dashboard: http://127.0.0.1:{settings.dashboard_port}" + ("  (demo data)" if demo else ""))
    try:
        await asyncio.Event().wait()
    finally:
        await dash.stop()


def _make_hooks_server(confirm, notify, say, brain, status):
    from smitty.hooks_server import HooksServer

    mgr = getattr(brain, "mgr", None)
    return HooksServer(settings.http_host, settings.http_port, confirm=confirm, notify=notify, say=say,
                       is_own_session=mgr.is_own_session if mgr else (lambda sid: False),
                       status=status, announce_idle=settings.announce_idle,
                       review=getattr(mgr, "review", None))


def cmd_shot(target: str, out: str | None, width: int, height: int, full: bool) -> None:
    """Take a phone-sized screenshot of a URL (or copy an image) into the artifacts folder and print its path."""
    from smitty import shot
    folder = Path(out).expanduser() if out else settings.data_dir / "artifacts" / "misc"
    print(shot.capture(target, folder, width=width, height=height, full=full))


async def cmd_review(target: str, question: str, session: str | None, project: str | None) -> int:
    """Ask the user to look at a screenshot (through the running Smitty) and print their answer.

    Meant for Claude Code sessions: `uv run smitty review http://localhost:3000 "Is the header right?"`.
    """
    import aiohttp
    body = {"target": target, "question": question, "session": session or f"the {Path.cwd().name} session",
            "project": project or Path.cwd().name}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=settings.review_wait_s + 120)) as http:
            async with http.post(f"http://127.0.0.1:{settings.http_port}/review", json=body) as r:
                data = await r.json()
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        print(f"Smitty is not running ({e}); nobody can look at it right now.", file=sys.stderr)
        return 2
    if data.get("error"):
        print(data["error"], file=sys.stderr)
        return 1
    print(data.get("answer", ""))
    return 0


async def cmd_run(brain_name: str) -> None:
    """Always-on Smitty: chat, inbox and schedules run the whole time; voice joins when the reSpeaker
    is plugged in and leaves when it is unplugged, in the same conversation."""
    if brain_name == "echo":
        return await _cmd_run_fixed("echo")          # the speak-it-back test needs the microphone up front
    import signal
    from smitty import store
    from smitty.voice import VoiceLink, usb_microphone

    wifi = settings.audio_transport in ("websocket", "ws", "wifi")
    att = _make_attention()
    brain = _make_claude_brain(att.confirm, att.notify, att)
    att.brain = brain
    loop = asyncio.get_running_loop()

    def make_pipeline(source, sink, leds):           # runs in a worker thread: loading models takes seconds
        from smitty.pipeline import VoicePipeline
        from smitty.tts import make_tts
        pipeline = VoicePipeline(settings, source, sink, brain=brain, status=leds)
        pipeline.tts = make_tts(settings)
        return pipeline

    def make_leds(source):
        if not settings.leds:
            return None
        from smitty.led import LedStatus, WsLedRing
        link = getattr(source, "_link", None)        # set for the Wi-Fi transport
        if link is None:
            return LedStatus()
        leds = LedStatus(WsLedRing(link, loop))
        link.on_ready.append(leds.reapply)           # re-show the state whenever the board reconnects
        return leds

    voice = VoiceLink(settings, brain, att, make_audio=_make_audio,
                      microphone=(lambda: (True, "")) if wifi else usb_microphone,
                      make_pipeline=make_pipeline, make_leds=make_leds, watch_frames=not wifi,
                      greeting=f"{settings.assistant_name} online.")
    brain.volume.apply = voice.apply_volume

    def systems() -> list[dict]:
        if voice.attached:
            return _systems("voice", brain, voice.pipeline.tts, voice.leds)
        return _systems("text", brain, note=voice.note)

    await brain.start()
    brain.scheduler.start()
    server = _make_hooks_server(att.confirm, att.notify, voice.say, brain, lambda: {
        "state": voice.state, "voice": voice.attached, "sessions": brain.mgr.describe_all()})
    control = _make_control(server, brain, att, "voice", base_state=lambda: voice.state)
    await server.start()
    started = time.time()
    store.put("systems", systems())
    store.put("state", "idle")
    store.put("approvals", [])
    store.put("chat", {"busy": False})
    store.record("system", f"{settings.assistant_name} started", mode="auto", transport=settings.audio_transport)
    beat = asyncio.create_task(_heartbeat(lambda: voice.mode, brain, started, control=True, systems=systems,
                                          extra=lambda: {"voice": voice.attached, "voice_note": voice.note, "auto_voice": True}))
    dash = await _start_dashboard() if settings.dashboard else None
    hub = _start_hub("auto")
    print(f"{settings.assistant_name} ready. Chat: http://127.0.0.1:{settings.dashboard_port}  "
          f"Voice turns on when the reSpeaker is plugged in. Ctrl-C to stop.", flush=True)
    main_task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    voice_task = asyncio.create_task(voice.run(), name="voice-link")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info("shutting down")
    finally:
        beat.cancel()
        voice_task.cancel()
        await asyncio.gather(voice_task, return_exceptions=True)
        store.record("system", f"{settings.assistant_name} stopped", mode="auto")
        store.put("state", "offline")
        control.close()
        await att.stop()
        if hub:
            await hub.stop()
        if dash:
            await dash.stop()
        voice.close()
        brain.scheduler.stop()
        await server.stop()
        await brain.stop()


async def _cmd_run_fixed(brain_name: str) -> None:
    """The original voice loop: the microphone must be there at start and stay."""
    from smitty.pipeline import EchoBrain, VoicePipeline
    from smitty.tts import make_tts

    source, sink = _make_audio()
    pipeline_ref: dict = {}

    async def confirm(question: str, **context) -> bool | None:
        return await pipeline_ref["p"].confirm(question)

    async def notify(event: dict) -> None:
        await pipeline_ref["p"].announce(event)

    async def say(text: str) -> None:
        await pipeline_ref["p"].say(text)

    # With the Claude brain, announcements and approvals go through the attention hub
    # (active: out loud + inbox; passive: inbox only). The echo brain talks to the pipeline directly.
    att = None
    if brain_name == "echo":
        brain = EchoBrain()
    else:
        att = _make_attention()
        confirm, notify = att.confirm, att.notify
        brain = _make_claude_brain(confirm, notify, att)
        att.brain = brain

    from smitty.led import LedStatus, WsLedRing

    leds = None
    if settings.leds:
        link = getattr(source, "_link", None)          # set for the Wi-Fi transport
        if link is not None:
            leds = LedStatus(WsLedRing(link, asyncio.get_running_loop()))
            link.on_ready.append(leds.reapply)         # re-show the state whenever the board reconnects
        else:
            leds = LedStatus()
    # Load models before opening the audio streams so no mic audio piles up.
    pipeline = VoicePipeline(settings, source, sink, brain=brain, status=leds)
    pipeline_ref["p"] = pipeline
    if att is not None:
        att.front, att.leds = pipeline, leds
    pipeline.tts = make_tts(settings)
    log.info("warming up whisper...")
    await asyncio.to_thread(pipeline.stt.warm_up)
    if hasattr(brain, "start"):
        await brain.start()
    if getattr(brain, "scheduler", None):
        brain.scheduler.start()
    server = _make_hooks_server(confirm, notify, say, brain, lambda: {
        "state": pipeline.state.value,
        "sessions": brain.mgr.describe_all() if hasattr(brain, "mgr") else [],
    })
    control = _make_control(server, brain, att, "voice", base_state=lambda: pipeline.state.value) if att is not None else None
    await server.start()
    from smitty import store
    started = time.time()
    store.put("systems", _systems("voice", brain, pipeline.tts, leds))
    store.put("approvals", [])
    store.record("system", f"{settings.assistant_name} started", mode="voice", transport=settings.audio_transport)
    beat = asyncio.create_task(_heartbeat("voice", brain, started, control=control is not None,
                                          systems=lambda: _systems("voice", brain, pipeline.tts, leds)))
    dash = await _start_dashboard() if settings.dashboard else None
    if leds:
        leds.start()
    # Ctrl-C arrives as KeyboardInterrupt; make SIGTERM (kill, launchd, supervisors) just as clean.
    import signal
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        async with source, sink:
            await pipeline.say(f"{settings.assistant_name} online.")
            await pipeline.run()
    except asyncio.CancelledError:
        log.info("shutting down")
    finally:
        beat.cancel()
        store.record("system", f"{settings.assistant_name} stopped", mode="voice")
        store.put("state", "offline")
        if control:
            control.close()
        if att is not None:
            await att.stop()
        if dash:
            await dash.stop()
        if leds:
            leds.stop()
        if getattr(brain, "scheduler", None):
            brain.scheduler.stop()
        await server.stop()
        if hasattr(brain, "stop"):
            await brain.stop()


async def cmd_chat() -> None:
    """Text REPL for the brain: no audio. Confirmations are answered on stdin (or in the dashboard)."""
    from smitty import store

    class Terminal:
        """Stands in for the voice pipeline: "speaks" by printing, "listens" on stdin."""
        async def confirm(self, question: str) -> bool | None:
            ans = await asyncio.to_thread(input, f"\n[CONFIRM] {question} (yes/no) > ")
            from smitty.brain.safety import parse_yes_no
            return parse_yes_no(ans)

        async def announce(self, event: dict) -> str:
            text = await brain.announce(event)
            store.record("said", text, source="announcement", about=event.get("title"))
            print(f"\n[{settings.assistant_name}] {text}\n> ", end="", flush=True)
            return text

    async def say(text: str) -> None:
        print(f"\n[{settings.assistant_name} says] {text}\n> ", end="", flush=True)

    att = _make_attention()
    brain = _make_claude_brain(att.confirm, att.notify, att)
    att.brain, att.front = brain, Terminal()
    await brain.start()
    brain.scheduler.start()
    server = _make_hooks_server(att.confirm, att.notify, say, brain, lambda: {"mode": "chat", "sessions": brain.mgr.describe_all()})
    control = _make_control(server, brain, att, "chat")
    await server.start()
    started = time.time()
    store.put("systems", _systems("chat", brain))
    store.put("state", "idle")
    store.put("approvals", [])
    store.record("system", f"{settings.assistant_name} started", mode="chat")
    beat = asyncio.create_task(_heartbeat("chat", brain, started, control=True, systems=lambda: _systems("chat", brain)))
    dash = await _start_dashboard() if settings.dashboard else None
    hub = _start_hub("chat")
    print(f"{settings.assistant_name} ready (text mode). Ctrl-D to quit.")
    try:
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            if not line.strip():
                continue
            store.record("heard", line.strip(), source="typed")
            store.put("state", "thinking")
            t0 = time.monotonic()
            reply = await brain.ask(line.strip())
            store.record("said", reply, source="reply", seconds=round(time.monotonic() - t0, 1))
            store.put("state", "idle")
            print(f"[{settings.assistant_name}] {reply}")
    finally:
        beat.cancel()
        store.record("system", f"{settings.assistant_name} stopped", mode="chat")
        store.put("state", "offline")
        control.close()
        await att.stop()
        if hub:
            await hub.stop()
        if dash:
            await dash.stop()
        brain.scheduler.stop()
        await server.stop()
        await brain.stop()


async def cmd_serve() -> None:
    """Text-only Smitty: brain, scheduler, hooks and dashboard, with no microphone or speaker.

    You talk to it in the dashboard chat; results and approvals arrive in the dashboard inbox.
    """
    import signal
    from smitty import store

    async def say(text: str) -> None:
        store.notice("message", "Message", text)

    att = _make_attention()                      # no front: nothing is ever spoken
    brain = _make_claude_brain(att.confirm, att.notify, att)
    att.brain = brain
    await brain.start()
    brain.scheduler.start()
    server = _make_hooks_server(att.confirm, att.notify, say, brain, lambda: {"mode": "text", "sessions": brain.mgr.describe_all()})
    control = _make_control(server, brain, att, "text")
    await server.start()
    started = time.time()
    store.put("systems", _systems("text", brain))
    store.put("state", "idle")
    store.put("approvals", [])
    store.put("chat", {"busy": False})
    store.record("system", f"{settings.assistant_name} started", mode="text")
    beat = asyncio.create_task(_heartbeat("text", brain, started, control=True, systems=lambda: _systems("text", brain)))
    dash = await _start_dashboard() if settings.dashboard else None
    hub = _start_hub("text")
    print(f"{settings.assistant_name} ready (text only). Dashboard: http://127.0.0.1:{settings.dashboard_port}  Ctrl-C to stop.", flush=True)
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info("shutting down")
    finally:
        beat.cancel()
        store.record("system", f"{settings.assistant_name} stopped", mode="text")
        store.put("state", "offline")
        control.close()
        await att.stop()
        if hub:
            await hub.stop()
        if dash:
            await dash.stop()
        brain.scheduler.stop()
        await server.stop()
        await brain.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="smitty")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list audio devices")
    p_say = sub.add_parser("say", help="speak text through the sink")
    p_say.add_argument("text")
    p_run = sub.add_parser("run", help="run Smitty: chat always, voice whenever the reSpeaker is plugged in")
    p_run.add_argument("--brain", default="claude", choices=["echo", "claude"])
    sub.add_parser("chat", help="talk to the brain in text (no audio)")
    sub.add_parser("serve", help="run without audio, even if the reSpeaker is plugged in")
    sub.add_parser("schedules", help="list scheduled tasks")
    p_dash = sub.add_parser("dashboard", help="serve the monitoring dashboard (http://127.0.0.1:8770)")
    p_dash.add_argument("--demo", action="store_true", help="show sample data instead of Smitty's real activity")
    p_shot = sub.add_parser("shot", help="phone-sized screenshot of a URL (headless Chrome) or copy of an image, into the artifacts folder")
    p_shot.add_argument("target", help="URL to render, or an image file")
    p_shot.add_argument("--out", help="folder to save into (default ~/.smitty/artifacts/misc)")
    p_shot.add_argument("--width", type=int, default=390)
    p_shot.add_argument("--height", type=int, default=844)
    p_shot.add_argument("--full", action="store_true", help="tall capture (2400px) for long pages")
    p_rev = sub.add_parser("review", help="ask the user to look at a screenshot via Smitty's inbox/phone; prints their reply")
    p_rev.add_argument("target", help="URL to render, or an image file")
    p_rev.add_argument("question")
    p_rev.add_argument("--session", help="how to name the asking session (default: the current folder)")
    p_rev.add_argument("--project")
    p_hub = sub.add_parser("hub", help="sync activity to the cloud hub for the phone app (runs inside `smitty run` too)")
    p_hub.add_argument("--check", action="store_true", help="do one sync and report, then exit")
    p_ws = sub.add_parser("ws-test", help="test the Wi-Fi (ESP32) audio link: play to it, record from it")
    p_ws.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpx2", "httpcore2", "huggingface_hub", "urllib3", "filelock", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.cmd in ("run", "chat", "serve"):
        if not _single_instance():
            return 1
        _apply_auth_policy()
        from smitty import store
        store.init(settings.data_dir / "smitty.db")
        store.prune()

    try:
        if args.cmd == "devices":
            from smitty.audio.usb import describe_devices
            print(describe_devices())
        elif args.cmd == "say":
            asyncio.run(cmd_say(args.text))
        elif args.cmd == "run":
            asyncio.run(cmd_run(args.brain))
        elif args.cmd == "chat":
            asyncio.run(cmd_chat())
        elif args.cmd == "serve":
            asyncio.run(cmd_serve())
        elif args.cmd == "ws-test":
            asyncio.run(cmd_ws_test(args.seconds))
        elif args.cmd == "dashboard":
            asyncio.run(cmd_dashboard(args.demo))
        elif args.cmd == "hub":
            asyncio.run(cmd_hub(args.check))
        elif args.cmd == "shot":
            cmd_shot(args.target, args.out, args.width, args.height, args.full)
        elif args.cmd == "review":
            return asyncio.run(cmd_review(args.target, args.question, args.session, args.project))
        elif args.cmd == "schedules":
            import json
            from smitty.scheduler import Scheduler
            print(json.dumps(Scheduler(None, settings.tasks_dir, settings.data_dir / "schedules.json").list(), indent=2))
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
