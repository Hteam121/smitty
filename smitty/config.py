"""Runtime settings. Override with env vars prefixed SMITTY_, set in the shell or in a .env file (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"


def _load_dotenv(path: Path) -> None:
    """Read SMITTY_* lines from a git-ignored .env next to the code. Real environment variables win."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key.startswith("SMITTY_"):
            os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default):
    raw = os.environ.get(f"SMITTY_{name}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass
class Settings:
    # Audio
    audio_transport: str = field(default_factory=lambda: _env("AUDIO", "usb"))  # usb | websocket
    volume: float = field(default_factory=lambda: _env("VOLUME", 0.5))  # master output gain, 0..1
    ws_host: str = "0.0.0.0"
    ws_port: int = field(default_factory=lambda: _env("WS_PORT", 8766))
    leds: bool = field(default_factory=lambda: _env("LEDS", True))  # drive the reSpeaker LED ring

    # Wake word (openWakeWord)
    wake_model: str = field(default_factory=lambda: _env("WAKE_MODEL", "hey_smitty"))
    wake_threshold: float = field(default_factory=lambda: _env("WAKE_THRESHOLD", 0.7))
    wake_refractory_s: float = 2.0
    wake_patience: int = 2  # consecutive hot frames required
    # What to do with the 2 s of audio that triggered the wake word:
    #   "log"    transcribe it in the background and log it (no latency; evidence for false wakes)
    #   "strict" transcribe before responding and ignore the wake if the text lacks the wake word (+0.3 s, risky)
    #   "off"    nothing
    wake_verify: str = field(default_factory=lambda: _env("WAKE_VERIFY", "strict"))
    wake_vad_threshold: float = 0.5  # the trigger window must contain speech (Silero prob) or it's noise
    wake_verify_pattern: str = r"s[cs]?h?m[iy]+[dt]+[iey]|smit+|smith|smid|schmit|shmit|mitty|smeet|smitti"

    # Endpointing (Silero VAD)
    vad_model_path: Path = MODELS_DIR / "silero_vad.onnx"
    vad_threshold: float = field(default_factory=lambda: _env("VAD_THRESHOLD", 0.5))
    vad_silence_ms: int = field(default_factory=lambda: _env("VAD_SILENCE_MS", 900))
    vad_no_speech_timeout_ms: int = 6000  # gave up waiting for the user to start
    vad_max_utterance_ms: int = 15_000
    vad_min_speech_ms: int = 300  # shorter than this is a cough, not a command
    vad_pre_roll_ms: int = 300

    # STT (mlx-whisper)
    whisper_model: str = field(
        default_factory=lambda: _env("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
    )
    whisper_language: str = "en"

    # TTS
    tts_engine: str = field(default_factory=lambda: _env("TTS", "kokoro"))  # kokoro | say
    kokoro_model_path: Path = MODELS_DIR / "kokoro-v1.0.onnx"
    kokoro_voices_path: Path = MODELS_DIR / "voices-v1.0.bin"
    kokoro_voice: str = field(default_factory=lambda: _env("VOICE", "bm_george"))
    kokoro_speed: float = 1.05
    say_voice: str = "Daniel"

    # Hooks HTTP endpoint
    http_host: str = "127.0.0.1"
    http_port: int = field(default_factory=lambda: _env("PORT", 8765))
    announce_idle: bool = field(default_factory=lambda: _env("ANNOUNCE_IDLE", False))  # idle_prompt notifications

    # Brain (Claude Agent SDK)
    # "login": use the claude.ai login that Claude Code already has (subscription).
    # "api_key": leave ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN alone and bill the API.
    auth: str = field(default_factory=lambda: _env("AUTH", "login"))
    assistant_name: str = "Smitty"
    user_name: str = field(default_factory=lambda: _env("USER_NAME", "the user"))  # what Smitty calls you
    brain_model: str = field(default_factory=lambda: _env("BRAIN_MODEL", "claude-opus-5-5"))
    brain_effort: str = field(default_factory=lambda: _env("BRAIN_EFFORT", "low"))
    worker_model: str | None = field(default_factory=lambda: _env("WORKER_MODEL", "") or None)
    # The Ego Lite browser profile sessions may use, and the only one (an id from `profiles()`, e.g. "Default" or "Profile 2").
    browser_profile: str = field(default_factory=lambda: _env("BROWSER_PROFILE", "Default"))
    project_roots: list[Path] = field(default_factory=lambda: [Path(p).expanduser() for p in _env("PROJECT_ROOTS", "~/Desktop").split(":")])
    confirm_timeout_s: float = 90.0  # must cover speaking the question + two 8 s listens
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "~/.smitty")).expanduser())
    tasks_dir: Path = field(default_factory=lambda: Path(_env("TASKS_DIR", "~/Desktop/smitty-tasks")).expanduser())
    # Obsidian vault (a folder of Markdown notes). Off until SMITTY_VAULT_DIR points at your vault.
    vault_dir: Path | None = field(default_factory=lambda: (lambda v: Path(v).expanduser() if v else None)(_env("VAULT_DIR", "")))
    schedule_min_interval_min: int = field(default_factory=lambda: _env("MIN_INTERVAL_MIN", 15))
    weather_units: str = field(default_factory=lambda: _env("UNITS", "f"))  # f | c
    dashboard_port: int = field(default_factory=lambda: _env("DASHBOARD_PORT", 8770))
    dashboard: bool = field(default_factory=lambda: _env("DASHBOARD", True))  # serve the dashboard while Smitty runs
    quiet_hours: str = field(default_factory=lambda: _env("QUIET_HOURS", ""))  # e.g. "23-7": hold scheduled results overnight
    # Attention: "active" speaks results and asks approvals out loud; "passive" only notifies in the dashboard.
    attention: str = field(default_factory=lambda: _env("ATTENTION", "active"))   # the mode used until you switch it
    approval_wait_s: float = field(default_factory=lambda: _env("APPROVAL_WAIT_S", 900.0))  # an unanswered approval is denied after this
    review_wait_s: float = field(default_factory=lambda: _env("REVIEW_WAIT_S", 1800.0))  # how long a session waits for you to look at a screenshot
    follow_up_timeout_ms: int = 4000  # how long to wait for a reply after Smitty asks a question
    # Cloud hub (Supabase) for the iPhone / Watch app: SMITTY_HUB_URL + SMITTY_HUB_KEY or ~/.smitty/hub.json.
    hub_url: str = field(default_factory=lambda: _env("HUB_URL", ""))
    hub_key: str = field(default_factory=lambda: _env("HUB_KEY", ""))
    hub_interval_s: float = field(default_factory=lambda: _env("HUB_INTERVAL_S", 3.0))


settings = Settings()
