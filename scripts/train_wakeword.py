"""Train a custom openWakeWord model for a phrase, entirely locally.

Positive and negative training clips are synthesized with Kokoro (28 English
voices x speeds), augmented with noise/reverb/gain/speed, embedded with
openWakeWord's frozen feature extractor, and a small classifier head is trained
with torch and exported by hand to ONNX in the layout openWakeWord expects
([N, 16, 96] -> [N, 1] sigmoid).

Held-out evaluation uses macOS `say` voices, a completely different TTS engine.

Usage:
    uv run scripts/train_wakeword.py --phrase "hey smitty"
    uv run scripts/train_wakeword.py --phrase "hey smitty" --eval-only
Writes models/<name>.onnx (name = phrase with underscores).
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import random
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np

from smitty.audio.base import SAMPLE_RATE, resample, to_int16
from smitty.config import MODELS_DIR, settings

warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")
log = logging.getLogger("train_wakeword")

WINDOW_S = 2.0
WINDOW = int(WINDOW_S * SAMPLE_RATE)
N_FRAMES = 16          # embedding frames the classifier sees
EMB_DIM = 96

KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
VAL_VOICES = ["af_sky", "am_puck", "bf_lily", "bm_fable"]
SAY_VOICES = ["Daniel", "Karen", "Moira", "Rishi", "Samantha", "Tessa", "Fred", "Kathy",
              "Ralph", "Albert", "Reed", "Eddy", "Flo", "Shelley", "Sandy", "Rocko"]

NEGATIVE_SENTENCES = [
    # everyday commands / speech
    "What time is it right now?", "Turn the lights off in the kitchen.", "Play some music please.",
    "Set a timer for ten minutes.", "What's the weather like tomorrow?", "Remind me to call mom.",
    "Open the project folder and run the tests.", "Start a new session in the backend repo.",
    "Resume the last session and tell it to fix the failing test.", "Yes.", "No.", "Yes, go ahead.",
    "No, cancel that.", "Thanks, that's all.", "Can you repeat that?", "I'm going to lunch now.",
    "The meeting starts at three.", "Did the build finish?", "Push it to the main branch.",
    "Delete the temporary files.", "How many sessions are running?", "Read me the last message.",
    "I think we should refactor the audio module.", "Let's add a websocket transport later.",
    "The microphone array has four channels.", "Echo cancellation needs the speaker reference.",
    "Please summarise the pull request.", "Approve the permission request.", "Deny it.",
    "Okay, sounds good.", "Hmm, let me think about that.", "Never mind.", "Stop.", "Hello?",
    "Is anyone there?", "Good morning.", "Good night.", "See you tomorrow.", "What did you say?",
    "I didn't catch that.", "Could you speak up?", "The quick brown fox jumps over the lazy dog.",
    "She sells sea shells by the sea shore.", "Peter Piper picked a peck of pickled peppers.",
    "Twenty two thirty three forty four.", "One two three four five six seven eight nine ten.",
    "The capital of France is Paris.", "Water boils at one hundred degrees celsius.",
    "My laptop battery is almost empty.", "Send the email to the whole team.",
    "Schedule the deploy for Friday afternoon.", "The tests are green now.", "It crashed again.",
    "Can you check the logs?", "Rotate the API keys next week.", "Where did I leave my keys?",
    "Order more coffee beans.", "The printer is out of paper.", "Lock the screen.",
    "Increase the volume.", "Decrease the brightness.", "Skip this track.", "Pause.", "Continue.",
    "I'll be back in five minutes.", "Tell me a joke.", "What's two plus two?", "Convert ten miles to kilometres.",
    "Translate hello into Spanish.", "Who wrote Hamlet?", "Spell necessary.", "Define serendipity.",
    # hard negatives: similar sounds, other wake words, partial phrase
    "Hey Siri.", "Hey Jarvis.", "Hey Google.", "Alexa.", "Okay Google.", "Hey Sammy.", "Hey Timmy.",
    "Hey Kitty.", "Hey Jimmy.", "Hey Smith.", "Hey, it's me.", "Hey there.", "Hey you.", "Hey.",
    "Say it again.", "Submit it.", "He's pretty.", "The city.", "Smitten.", "The smithy is hot.",
    "Hey, is it ready?", "Hey, wait a minute.", "They said it was easy.", "Hey mister.",
    "A pretty kitty.", "Hey, listen.", "Hey buddy.", "Hey Sweetie.", "Hey Betty.", "Hey Mickey.",
    "Hey Smithers.", "Hey, sit here.", "Spit it out.", "Mitty.", "Smitty's here.", "Where's Smitty?",
]


HEY_NEGATIVE_WORDS = [
    # phonetically close names
    "Misty", "Missy", "Mindy", "Cindy", "Sydney", "Kitty", "Smithers", "Smith", "Sweetie", "Timmy",
    "Jimmy", "Vinnie", "Minnie", "Ziggy", "Biddy", "Sid", "Mitzi", "Mitch", "Smokey", "Sammy",
    "Simon", "Sandy", "Sunny", "Snitty", "Smelly", "Pity", "Witty", "Kiddy", "Buddy", "Teddy",
    "Freddy", "Eddie", "Betty", "Patty", "Mickey", "Nicky", "Ricky", "Vicky", "Lizzy", "Izzy",
    "Smitten", "Spitty", "Shitty", "Sitting", "Skippy", "Scotty", "Snoopy", "Swifty", "Stevie", "Sally",
    # other assistants / common names
    "Siri", "Jarvis", "Google", "Alexa", "Cortana", "Computer", "Friday", "Peter", "Mandy", "Jenny",
    "Tom", "Anna", "Mike", "Sarah", "David", "Emma", "Chris", "Lisa", "James", "Mary",
    "Mom", "Dad", "Dude", "Man", "Bro", "Guys", "Everyone", "Boss", "Doctor", "Captain",
    # hey + non-name continuations
    "listen", "wait", "stop", "come here", "look at this", "is it ready", "what's up", "how are you",
    "there", "you", "hello", "hi", "yo", "now", "no", "yes", "please", "sorry", "thanks", "okay",
    "it's me", "I'm home", "let's go", "check this out", "can you hear me", "one second", "hold on",
    "what time is it", "I need help", "that's it", "it's ready", "it's done", "did it work", "guess what",
]

# "Hey, is it ..." family: shares the "hey" + vowel onset with "hey smitty" and used to score ~1.0.
# Deliberately excludes the exact evaluation phrase ("is it ready yet").
HEY_ISIT_OPENERS = ["Hey, is it", "Hey, it's", "Hey, isn't it", "Hey, is this", "Hey, it is"]
HEY_ISIT_ENDINGS = [
    "working", "time to go", "raining outside", "ready already", "done now", "too late", "the right one",
    "on", "broken again", "my turn", "cold in here", "supposed to do that", "still loading", "lunch yet",
    "going to work", "mine", "weird", "the weekend", "late", "fixed", "a good idea", "plugged in",
    "almost finished", "really that easy", "empty", "open", "true", "snowing", "getting dark",
]


# "...ready yet" ends on a Smitty-like "-dy yet / -tty" rhythm; the classifier fires on the tail of the utterance
HEY_ISIT_YET_ENDINGS = ["done yet", "there yet", "working yet", "fixed yet", "open yet", "finished yet",
                        "ready yet", "ready now", "ready already", "ready to go", "ready, buddy", "steady yet", "empty yet",
                        "time yet", "dry yet", "heavy yet", "ready soon"]


def heyisit_texts(n: int = 60, seed: int = 3) -> list[str]:
    rng = random.Random(seed)
    combos = [f"{o} {e}{p}" for o in HEY_ISIT_OPENERS for e in HEY_ISIT_ENDINGS for p in ("?", ".")]
    yet = [f"{o} {e}{p}" for o in HEY_ISIT_OPENERS for e in HEY_ISIT_YET_ENDINGS for p in ("?", ".")]
    # the phrase seen in the wild ("hey, is it ready yet") is always included, with every opener
    must = [f"{o} ready yet{p}" for o in HEY_ISIT_OPENERS[:2] for p in ("?", ".")]
    rest = [t for t in yet if "ready yet" not in t]
    return must + rng.sample(combos, n - 24 - len(must)) + rng.sample(rest, 24)


# Snippets of background speech (ASR transcripts) that falsely woke the assistant in daily use, 2026-10-19.
BACKGROUND_REPORTED = ["audible", "IL", "Oh", "in the morning", "burial compositions"]
# Similar-sounding phrases (same vowels / syllable rhythm / "-ible", "-ial", "-ing" endings) used as extra negatives.
BACKGROUND_SIMILAR = [
    "inaudible", "adorable", "available", "incredible", "edible", "terrible", "horrible", "possible", "visible",
    "ill", "isle", "I'll", "eel", "all", "oil", "owl", "oh no", "oh okay", "oh yeah", "oh well", "uh oh", "ooh", "ohh",
    "in the evening", "in the mourning", "this morning", "in the meantime", "in a minute", "in the middle",
    "burial", "buried", "memorial compositions", "serial positions", "bureau compositions", "burial grounds",
    "musical compositions", "official positions", "material conditions", "imperial composition",
    "very quickly", "merry Christmas", "a little bit", "pretty good", "it's a pity", "sit with it",
]


BG_ENDALIGNED = int(os.environ.get("WAKEWORD_BG_ENDALIGNED", 3))   # end-aligned copies per background negative
RUNON_FRAC = 0.4                      # fraction of positive windows followed by more speech
RUNON_COMMANDS = ["show me the lights", "what time is it", "turn off the kitchen lights", "play some music",
                  "set a timer for ten minutes", "open the project folder", "what's the weather tomorrow",
                  "read me the last message"]


# --------------------------------------------------------------------------- synthesis
_kokoro = None


def _init_worker():
    global _kokoro
    from kokoro_onnx import Kokoro
    _kokoro = Kokoro(str(settings.kokoro_model_path), str(settings.kokoro_voices_path))


def _synth(job):
    text, voice, speed = job
    samples, sr = _kokoro.create(text, voice=voice, speed=speed, lang="en-us")
    audio = resample(np.asarray(samples, dtype=np.float32), sr, SAMPLE_RATE)
    return trim_silence(audio)


def trim_silence(x: np.ndarray, thresh: float = 0.01, pad_s: float = 0.05) -> np.ndarray:
    idx = np.where(np.abs(x) > thresh)[0]
    if len(idx) == 0:
        return x
    pad = int(pad_s * SAMPLE_RATE)
    return x[max(0, idx[0] - pad): min(len(x), idx[-1] + pad)]


def synth_all(jobs: list[tuple[str, str, float]], workers: int = 4) -> list[np.ndarray]:
    t0 = time.monotonic()
    with mp.get_context("spawn").Pool(workers, initializer=_init_worker) as pool:
        out = []
        for i, clip in enumerate(pool.imap(_synth, jobs, chunksize=4)):
            out.append(clip)
            if (i + 1) % 50 == 0:
                print(f"\r    synthesized {i+1}/{len(jobs)}", end="", flush=True)
    print(f"\r    synthesized {len(jobs)} clips in {time.monotonic()-t0:.0f}s")
    return out


def say16k(text: str, voice: str) -> np.ndarray:
    from smitty.tts import MacSayTTS
    pcm, sr = MacSayTTS(voice=voice).synthesize_sync(text)
    return trim_silence(resample(pcm, sr, SAMPLE_RATE))


# --------------------------------------------------------------------------- noise / augmentation
def record_room_noise(seconds: float = 20.0) -> np.ndarray | None:
    try:
        import sounddevice as sd
        from smitty.audio.usb import device_name, find_device, mac_devices
        # The microphone Smitty will listen on: the reSpeaker when it is plugged in, else the Mac's own.
        idx = find_device()
        if idx is None:
            idx = mac_devices(settings.mic, settings.speaker)[0]
        if idx is None:
            return None
        print(f"    recording {seconds:.0f}s of room noise from {device_name(idx)} (stay quiet-ish)...")
        channels = min(2, int(sd.query_devices(idx)["max_input_channels"]))
        rec = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=channels, dtype="int16", device=idx)
        sd.wait()
        return rec[:, 0].astype(np.float32) / 32768.0
    except Exception as e:
        log.warning("room noise recording failed: %s", e)
        return None


def colored_noise(n: int, rng: np.random.Generator, kind: str) -> np.ndarray:
    white = rng.standard_normal(n).astype(np.float32)
    if kind == "white":
        x = white
    else:
        spec = np.fft.rfft(white)
        f = np.arange(len(spec), dtype=np.float32)
        f[0] = 1.0
        spec = spec / (f ** (0.5 if kind == "pink" else 1.0))
        x = np.fft.irfft(spec, n).astype(np.float32)
    return x / (np.abs(x).max() + 1e-6)


class Augmenter:
    def __init__(self, noises: list[np.ndarray], babble_pool: list[np.ndarray], seed: int = 0):
        self.rng = np.random.default_rng(seed)
        self.noises = noises
        self.babble = babble_pool

    def noise_window(self, n: int) -> np.ndarray:
        r = self.rng
        kind = r.choice(["room", "room", "white", "pink", "brown", "babble", "silence"])
        if kind == "silence":
            return r.standard_normal(n).astype(np.float32) * 1e-4
        if kind == "babble" and self.babble:
            out = np.zeros(n, np.float32)
            for _ in range(3):
                c = self.babble[r.integers(len(self.babble))]
                if len(c) >= n:
                    s = r.integers(0, len(c) - n + 1)
                    out += c[s:s+n]
                else:
                    s = r.integers(0, n - len(c) + 1)
                    out[s:s+len(c)] += c
            return out / (np.abs(out).max() + 1e-6)
        if kind == "room" and self.noises:
            src = self.noises[r.integers(len(self.noises))]
            if len(src) >= n:
                s = r.integers(0, len(src) - n + 1)
                return src[s:s+n]
        return colored_noise(n, r, kind if kind in ("white", "pink", "brown") else "pink")

    def reverb(self, x: np.ndarray) -> np.ndarray:
        r = self.rng
        t60 = r.uniform(0.15, 0.6)
        n = int(t60 * SAMPLE_RATE)
        rir = r.standard_normal(n).astype(np.float32) * np.exp(-6.9 * np.arange(n) / n)
        rir[0] = 1.0
        rir /= np.abs(rir).sum() ** 0.5
        y = np.convolve(x, rir)[: len(x)]
        return y / (np.abs(y).max() + 1e-6) * np.abs(x).max()

    def speech_variant(self, clip: np.ndarray) -> np.ndarray:
        r = self.rng
        x = clip / (np.abs(clip).max() + 1e-6)
        rate = r.uniform(0.85, 1.18)
        x = resample(x, SAMPLE_RATE, int(SAMPLE_RATE * rate))  # time-stretch via resampling
        if r.random() < 0.5:    # close the Kokoro -> other-TTS/mic gap: random band-limit / tilt
            from scipy.signal import butter, lfilter
            if r.random() < 0.6:
                b, a = butter(2, r.uniform(2500, 7000) / (SAMPLE_RATE / 2), "low")
            else:
                b, a = butter(1, r.uniform(150, 600) / (SAMPLE_RATE / 2), "high")
            x = lfilter(b, a, x).astype(np.float32)
        if r.random() < 0.35:
            x = self.reverb(x)
        return x * r.uniform(0.25, 1.0)

    def place(self, x: np.ndarray, end_offset_s: float) -> np.ndarray:
        """Put speech x into a WINDOW-long buffer so it ends end_offset_s before the end."""
        end = WINDOW - int(end_offset_s * SAMPLE_RATE)
        start = end - len(x)
        out = np.zeros(WINDOW, np.float32)
        if start < 0:
            x = x[-start:]
            start = 0
        out[start:end] = x[: end - start]
        return out

    def place_runon(self, x: np.ndarray, follow: np.ndarray) -> np.ndarray:
        """Wake phrase x followed (0-250 ms gap) by more speech, phrase ending 0.3-1.2 s before the window end."""
        r = self.rng
        f = follow[: int(r.uniform(0.5, 1.0) * SAMPLE_RATE)] if r.random() < 0.5 else follow
        f = self.speech_variant(f)
        f = f / (np.abs(f).max() + 1e-6) * np.abs(x).max() * r.uniform(0.5, 1.1)
        # The classifier only sees the last N_FRAMES embeddings (~1.25 s), so the whole phrase must start inside
        # that span or the "positive" would be unobservable and the model learns to fire on any speech.
        hi = float(np.clip(1.25 - len(x) / SAMPLE_RATE, 0.35, 1.2))
        end = WINDOW - int(r.uniform(0.12, hi) * SAMPLE_RATE)
        out = np.zeros(WINDOW, np.float32)
        start = end - len(x)
        if start < 0:
            x, start = x[-start:], 0
        out[start:end] = x
        fs = end + int(r.uniform(0.0, 0.25) * SAMPLE_RATE)
        n = min(len(f), WINDOW - fs)
        if n > 0:
            out[fs:fs + n] = f[:n]
        return out

    def mix(self, speech: np.ndarray, snr_db: float | None = None) -> np.ndarray:
        r = self.rng
        snr_db = r.uniform(3, 30) if snr_db is None else snr_db
        noise = self.noise_window(len(speech))
        sp = np.sqrt(np.mean(speech ** 2)) + 1e-6
        npow = np.sqrt(np.mean(noise ** 2)) + 1e-6
        y = speech + noise * (sp / npow) / (10 ** (snr_db / 20))
        peak = np.abs(y).max()
        return y / peak * r.uniform(0.3, 0.95) if peak > 1 else y


# --------------------------------------------------------------------------- features
def embed(windows: np.ndarray) -> np.ndarray:
    """[N, WINDOW] float -> [N, 16, 96] features via openWakeWord's extractor."""
    from openwakeword.utils import AudioFeatures
    af = AudioFeatures(ncpu=4)
    x = to_int16(np.clip(windows, -1, 1))
    feats = af.embed_clips(x, batch_size=64, ncpu=4)
    return feats[:, -N_FRAMES:, :].astype(np.float32)


# --------------------------------------------------------------------------- model
def build_torch_model():
    import torch.nn as nn
    return nn.Sequential(
        nn.Flatten(), nn.Dropout(0.2),
        nn.Linear(N_FRAMES * EMB_DIM, 128), nn.LayerNorm(128), nn.ReLU(), nn.Dropout(0.3),
        nn.Linear(128, 128), nn.LayerNorm(128), nn.ReLU(), nn.Dropout(0.3),
        nn.Linear(128, 1),
    )


def train(X, y, Xv, yv, epochs: int = 40, seed: int = 0, pos_boost: float = 1.0):
    import torch
    torch.manual_seed(seed)
    model = build_torch_model()
    Xt, yt = torch.tensor(X), torch.tensor(y, dtype=torch.float32)
    Xvt, yvt = torch.tensor(Xv), torch.tensor(yv, dtype=torch.float32)
    pos_w = torch.tensor([pos_boost * (len(y) - y.sum()) / max(1, y.sum())])
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_w)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    best, best_state = -1.0, None
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(Xt))
        for i in range(0, len(perm), 256):
            idx = perm[i:i+256]
            xb = Xt[idx] + torch.randn_like(Xt[idx]) * 0.02  # feature-space jitter
            opt.zero_grad()
            loss = loss_fn(model(xb).squeeze(1), yt[idx])
            loss.backward()
            opt.step()
        sched.step()
        model.eval()
        with torch.no_grad():
            pv = torch.sigmoid(model(Xvt).squeeze(1)).numpy()
        tp = ((pv >= 0.5) & (yv == 1)).sum(); fn = ((pv < 0.5) & (yv == 1)).sum()
        fp = ((pv >= 0.5) & (yv == 0)).sum(); tn = ((pv < 0.5) & (yv == 0)).sum()
        recall = tp / max(1, tp + fn); fpr = fp / max(1, fp + tn)
        score = recall - 5 * fpr
        if score > best:
            best, best_state = score, {k: v.clone() for k, v in model.state_dict().items()}
        print(f"    epoch {ep+1:2d}  loss {loss.item():.3f}  val recall {recall:.3f}  val FPR {fpr:.4f}")
    model.load_state_dict(best_state)
    print(f"    seed {seed}: best val score {best:.3f}")
    return model, best


def export_onnx(models, path: Path) -> None:
    """Hand-build the ONNX graph so it matches openWakeWord's expected IO exactly.

    `models` is one network or a list: several seeds are averaged (mean of sigmoids), which damps
    the single-network spikes that fire on unrelated speech.
    """
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    import torch.nn as nn
    if not isinstance(models, (list, tuple)):
        models = [models]
    inits, nodes, outs = [], [], []

    def W(name, t):
        inits.append(numpy_helper.from_array(np.ascontiguousarray(t.detach().numpy().astype(np.float32)), name))
        return name

    nodes.append(helper.make_node("Flatten", ["x"], ["flat"], axis=1))
    for mi, model in enumerate(models):
        prev, k = "flat", 0
        mods = [m for m in model if not isinstance(m, (nn.Flatten, nn.Dropout))]
        for m in mods:
            out = f"m{mi}_t{k}"
            if isinstance(m, nn.Linear):
                nodes.append(helper.make_node("Gemm", [prev, W(f"m{mi}_W{k}", m.weight), W(f"m{mi}_b{k}", m.bias)], [out], transB=1))
            elif isinstance(m, nn.LayerNorm):
                nodes.append(helper.make_node("LayerNormalization", [prev, W(f"m{mi}_g{k}", m.weight), W(f"m{mi}_beta{k}", m.bias)], [out], axis=-1, epsilon=float(m.eps)))
            elif isinstance(m, nn.ReLU):
                nodes.append(helper.make_node("Relu", [prev], [out]))
            else:
                raise TypeError(f"unsupported layer {m}")
            prev, k = out, k + 1
        nodes.append(helper.make_node("Sigmoid", [prev], [f"m{mi}_p"]))
        outs.append(f"m{mi}_p")
    if len(outs) == 1:
        nodes.append(helper.make_node("Identity", outs, ["y"]))
    else:
        nodes.append(helper.make_node("Concat", outs, ["cat"], axis=1))
        nodes.append(helper.make_node("ReduceMean", ["cat"], ["y"], axes=[1], keepdims=1))

    graph = helper.make_graph(
        nodes, "wakeword",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["N", N_FRAMES, EMB_DIM])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, ["N", 1])],
        initializer=inits,
    )
    m = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], producer_name="smitty")
    m.ir_version = 8
    onnx.checker.check_model(m)
    onnx.save(m, str(path))


# --------------------------------------------------------------------------- evaluation
def streaming_max_score(onnx_path: Path, audio: np.ndarray) -> float:
    from openwakeword.model import Model
    m = Model(wakeword_model_paths=[str(onnx_path)])
    key = list(m.models.keys())[0]
    x = to_int16(np.concatenate([np.zeros(SAMPLE_RATE // 2, np.float32), audio, np.zeros(SAMPLE_RATE, np.float32)]))
    best = 0.0
    for i in range(0, len(x) - 1280 + 1, 1280):
        best = max(best, float(m.predict(x[i:i+1280])[key]))
    return best


def streaming_scores(onnx_path: Path, audio: np.ndarray) -> np.ndarray:
    from openwakeword.model import Model
    m = Model(wakeword_model_paths=[str(onnx_path)])
    key = list(m.models.keys())[0]
    x = to_int16(audio)
    return np.array([float(m.predict(x[i:i+1280])[key]) for i in range(0, len(x) - 1280 + 1, 1280)])


def evaluate(onnx_path: Path, phrase: str, aug: Augmenter, threshold: float) -> dict:
    print("  [eval] held-out macOS `say` voices saying the phrase:")
    hits = 0
    for v in SAY_VOICES:
        try:
            clip = say16k(phrase, v)
        except Exception as e:
            print(f"    {v:<10} (say failed: {e})"); continue
        clean = streaming_max_score(onnx_path, clip)
        noisy = streaming_max_score(onnx_path, aug.mix(np.concatenate([np.zeros(8000, np.float32), clip, np.zeros(8000, np.float32)]), snr_db=10))
        ok = clean >= threshold
        hits += ok
        print(f"    {v:<10} clean {clean:.3f}  noisy(10dB) {noisy:.3f}  {'HIT' if ok else 'miss'}")
    recall = hits / len(SAY_VOICES)

    print("  [eval] run-on recall (phrase + command, no pause; streaming max >= 0.6):")
    runon_hits = 0
    for i, v in enumerate(SAY_VOICES):
        cmd = RUNON_COMMANDS[i % len(RUNON_COMMANDS)]
        try:
            clip = say16k(f"{phrase} {cmd}", v)
        except Exception as e:
            print(f"    {v:<10} (say failed: {e})"); continue
        sc = streaming_max_score(onnx_path, clip)
        ok = sc >= 0.6
        runon_hits += ok
        print(f"    {v:<10} {sc:.3f}  {'HIT' if ok else 'miss'}  \"{phrase} {cmd}\"")

    first = phrase.split()[0]
    print(f"  [eval] \"{first}, is it ready yet\" and cousins (want < 0.6):")
    isit_ok = 0
    for v in SAY_VOICES[:8]:
        sc = streaming_max_score(onnx_path, say16k(f"{first}, is it ready yet", v))
        isit_ok += sc < 0.6
        print(f"    {v:<10} {sc:.3f}  {'ok' if sc < 0.6 else 'FALSE FIRE'}")
    for t in (f"{first}, it's working", f"{first}, is this on", f"{first}, isn't it late", f"{first}, it is time"):
        scs = [streaming_max_score(onnx_path, say16k(t, v)) for v in SAY_VOICES[:8]]
        print(f"    extra \"{t}\": max {max(scs):.3f}, >=0.6 for {sum(x >= 0.6 for x in scs)}/8 voices")

    print(f"  [eval] reported background snippets, {len(BACKGROUND_REPORTED)} + {len(BACKGROUND_SIMILAR)} similar x 8 voices (want < 0.5):")
    bg_stats = {}
    for label, words_ in (("reported", BACKGROUND_REPORTED), ("similar", BACKGROUND_SIMILAR)):
        fires, total, worst = [], 0, 0.0
        for w in words_:
            scs = [streaming_max_score(onnx_path, say16k(w, v)) for v in SAY_VOICES[:8]]
            total += len(scs); worst = max(worst, max(scs))
            n_hot = sum(x >= 0.5 for x in scs)
            if n_hot:
                fires.append(f"{w!r} {n_hot}/8 (max {max(scs):.2f})")
        print(f"    {label}: {len(fires)}/{len(words_)} phrases trigger on >=1 voice; max score {worst:.3f}")
        for f in fires:
            print(f"      TRIGGER {f}")
        bg_stats[label] = {"phrases_triggering": len(fires), "phrases": len(words_), "max": worst}

    print("  [eval] false positives on `say` negatives + noise:")
    rng = random.Random(1)
    neg_audio = [np.zeros(SAMPLE_RATE, np.float32)]
    for v in SAY_VOICES[:8]:
        for s in rng.sample(NEGATIVE_SENTENCES, 6):
            neg_audio.append(say16k(s, v)); neg_audio.append(np.zeros(4000, np.float32))
    neg_audio.append(aug.noise_window(SAMPLE_RATE * 30))
    neg = np.concatenate(neg_audio)
    scores = streaming_scores(onnx_path, neg)
    fp = int((scores >= threshold).sum())
    for th in (0.5, 0.7, 0.9):
        hot = scores >= th
        single = int(hot.sum())
        double = int((hot[1:] & hot[:-1]).sum())
        print(f"    {len(neg)/SAMPLE_RATE:.0f}s of negatives @ {th}: {single} hot frames, {double} with 2-frame patience")
    print(f"    max negative score {scores.max():.3f}")
    return {"say_recall": recall, "runon_recall": runon_hits / len(SAY_VOICES), "heyisit_ok_voices": isit_ok, "background": bg_stats,
            "neg_false_frames": fp, "neg_max_score": float(scores.max()), "neg_seconds": len(neg) / SAMPLE_RATE}


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phrase", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--seeds", type=int, default=5, help="train this many seeds and average them in the exported model")
    ap.add_argument("--pos-boost", type=float, default=1.0, help="extra weight on positives (trades false accepts for recall)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    phrase = args.phrase.strip()
    name = args.name or phrase.lower().replace(" ", "_")
    data_dir = MODELS_DIR / "wakeword_data" / name
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = MODELS_DIR / f"{name}.onnx"

    # --- room noise -----------------------------------------------------------
    noise_path = data_dir.parent / "room_noise.npy"
    if noise_path.exists():
        room = np.load(noise_path)
    else:
        room = record_room_noise()
        if room is not None:
            np.save(noise_path, room)
    noises = [room] if room is not None else []

    # --- synthesize --------------------------------------------------------
    pos_path, neg_path = data_dir / "positives.npz", data_dir / "negatives.npz"
    words = phrase.split()
    spellings = [phrase]
    if words and words[-1].lower().endswith("tty"):      # smitty -> smittie / smiddy (flapped t)
        base = words[-1][:-3]
        spellings += [" ".join(words[:-1] + [base + "ttie"]), " ".join(words[:-1] + [base + "ddy"])]
    pos_texts = [f"{s.capitalize()}." for s in spellings] + [f"{phrase.capitalize()}!", f"{phrase.capitalize()}?", f"{words[0].capitalize()}, {' '.join(words[1:])}."]
    pos_texts = list(dict.fromkeys(pos_texts))

    if not args.eval_only and not pos_path.exists():
        print(f"[1/5] synthesizing positives: {pos_texts}")
        jobs = [(t, v, s) for t in pos_texts for v in KOKORO_VOICES for s in (0.85, 1.0, 1.15, 1.3)]
        clips = synth_all(jobs, args.workers)
        np.savez_compressed(pos_path, clips=np.array(clips, dtype=object), voices=[j[1] for j in jobs])
    if not args.eval_only and not neg_path.exists():
        print(f"[2/5] synthesizing {len(NEGATIVE_SENTENCES)} negative sentences x 3")
        rng = random.Random(0)
        jobs = [(s, rng.choice(KOKORO_VOICES), rng.choice((0.85, 1.0, 1.15, 1.3))) for s in NEGATIVE_SENTENCES for _ in range(3)]
        clips = synth_all(jobs, args.workers)
        np.savez_compressed(neg_path, clips=np.array(clips, dtype=object), voices=[j[1] for j in jobs])

    hey_path = data_dir / "negatives_hey.npz"
    if not args.eval_only and not hey_path.exists():
        hey_texts = [f"{words[0].capitalize()} {w}." for w in HEY_NEGATIVE_WORDS] + [f"{words[0].capitalize()}, {w}!" for w in HEY_NEGATIVE_WORDS]
        print(f"[2b/5] synthesizing {len(hey_texts)} '{words[0]} ___' hard negatives x 2")
        rng = random.Random(1)
        jobs = [(t, rng.choice(KOKORO_VOICES), rng.choice((0.9, 1.0, 1.15, 1.3))) for t in hey_texts for _ in range(2)]
        clips = synth_all(jobs, args.workers)
        np.savez_compressed(hey_path, clips=np.array(clips, dtype=object), voices=[j[1] for j in jobs])

    isit_path = data_dir / "negatives_heyisit.npz"
    if not args.eval_only and not isit_path.exists():
        isit_texts = heyisit_texts()
        print(f"[2c/5] synthesizing {len(isit_texts)} '{words[0]}, is it ...' hard negatives x 3 (8 for 'ready yet')")
        rng = random.Random(2)
        jobs = [(t, rng.choice(KOKORO_VOICES), rng.choice((0.9, 1.0, 1.15, 1.3)))
                for t in isit_texts for _ in range(8 if "ready yet" in t else 3)]
        clips = synth_all(jobs, args.workers)
        np.savez_compressed(isit_path, clips=np.array(clips, dtype=object), voices=[j[1] for j in jobs])

    bg_path = data_dir / "negatives_background.npz"
    if not args.eval_only and not bg_path.exists():
        bg_texts = [t for w in BACKGROUND_REPORTED + BACKGROUND_SIMILAR for t in (f"{w}.", w[0].lower() + w[1:] if len(w) > 2 else w)]
        print(f"[2d/5] synthesizing {len(bg_texts)} background-speech hard negatives x 4")
        rng = random.Random(4)
        jobs = [(t, rng.choice(KOKORO_VOICES), rng.choice((0.85, 1.0, 1.15, 1.3))) for t in bg_texts for _ in range(4)]
        clips = synth_all(jobs, args.workers)
        np.savez_compressed(bg_path, clips=np.array(clips, dtype=object), voices=[j[1] for j in jobs])

    if not args.eval_only:
        pos = np.load(pos_path, allow_pickle=True); neg = np.load(neg_path, allow_pickle=True)
        hey = np.load(hey_path, allow_pickle=True)
        isit = np.load(isit_path, allow_pickle=True)
        bg = np.load(bg_path, allow_pickle=True)
        pos_clips, pos_voices = list(pos["clips"]), list(pos["voices"])
        follow_pool = list(neg["clips"])      # sentences that can trail a run-on wake phrase
        follow_by_voice: dict[str, list] = {}
        for c, v in zip(neg["clips"], neg["voices"]):
            follow_by_voice.setdefault(str(v), []).append(c)   # a real run-on is one speaker
        # (clip, voice, n_variants); only the "is it" family is end-aligned, near-names like Smithers would fight the positives
        hard = [(c, v, 4) for c, v in zip(isit["clips"], isit["voices"])] \
               + [(c, v, BG_ENDALIGNED) for c, v in zip(bg["clips"], bg["voices"])]
        neg_clips = list(neg["clips"]) + list(hey["clips"]) + list(isit["clips"]) + list(bg["clips"])
        neg_voices = list(neg["voices"]) + list(hey["voices"]) + list(isit["voices"]) + list(bg["voices"])
        # time-reversed speech: speech-like, never the phrase
        neg_clips += [c[::-1] for c in pos_clips[::3]] + [c[::-1] for c in neg_clips[::4]]
        neg_voices += list(pos_voices[::3]) + list(neg_voices[::4])
        aug = Augmenter(noises, babble_pool=neg_clips, seed=0)

        # --- build windows ----------------------------------------------------
        print("[3/5] augmenting")
        X_tr, y_tr, X_va, y_va = [], [], [], []
        for clip, voice in zip(pos_clips, pos_voices):
            dst = (X_va, y_va) if voice in VAL_VOICES else (X_tr, y_tr)
            same_voice = follow_by_voice.get(str(voice)) or follow_pool
            for _ in range(6):
                x = aug.speech_variant(clip)
                if aug.rng.random() < RUNON_FRAC:
                    pool = same_voice if aug.rng.random() < 0.7 else follow_pool
                    w = aug.mix(aug.place_runon(x, pool[aug.rng.integers(len(pool))]))
                else:
                    w = aug.mix(aug.place(x, aug.rng.uniform(0.05, 0.45)))
                dst[0].append(w); dst[1].append(1)
        for clip, voice in zip(neg_clips, neg_voices):
            dst = (X_va, y_va) if voice in VAL_VOICES else (X_tr, y_tr)
            x = aug.speech_variant(clip)
            padded = np.concatenate([np.zeros(WINDOW // 2, np.float32), x, np.zeros(WINDOW // 2, np.float32)])
            for s in range(0, max(1, len(padded) - WINDOW), int(0.5 * SAMPLE_RATE)):
                dst[0].append(aug.mix(padded[s:s+WINDOW])); dst[1].append(0)
        # hard negatives placed exactly like positives (ending 0.05-0.45 s before the window end, or run-on style)
        for clip, voice, n_var in hard:
            dst = (X_va, y_va) if voice in VAL_VOICES else (X_tr, y_tr)
            for k in range(n_var):
                x = aug.speech_variant(clip)
                if k:   # streaming also sees the utterance cut off part-way, e.g. just "hey, is it"
                    x = x[: int(len(x) * aug.rng.uniform(0.3, 0.9))]
                if k % 2:
                    f = follow_pool[aug.rng.integers(len(follow_pool))]
                    w = aug.place_runon(x, f)
                else:
                    w = aug.place(x, aug.rng.uniform(0.05, 0.45))
                dst[0].append(aug.mix(w)); dst[1].append(0)
        for _ in range(600):   # pure noise / silence windows
            X_tr.append(aug.noise_window(WINDOW) * aug.rng.uniform(0.05, 0.9)); y_tr.append(0)
        for _ in range(100):
            X_va.append(aug.noise_window(WINDOW) * aug.rng.uniform(0.05, 0.9)); y_va.append(0)
        X_tr, y_tr, X_va, y_va = (np.stack(X_tr), np.array(y_tr), np.stack(X_va), np.array(y_va))
        print(f"    train: {len(y_tr)} windows ({int(y_tr.sum())} pos)   val: {len(y_va)} windows ({int(y_va.sum())} pos)")

        print("[4/5] extracting openWakeWord features")
        t0 = time.monotonic()
        F_tr, F_va = embed(X_tr), embed(X_va)
        print(f"    features {F_tr.shape} in {time.monotonic()-t0:.0f}s")

        if os.environ.get("WAKEWORD_DUMP_FEATURES"):
            np.savez(os.environ["WAKEWORD_DUMP_FEATURES"], F_tr=F_tr, y_tr=y_tr, F_va=F_va, y_va=y_va)
            if os.environ.get("WAKEWORD_DUMP_ONLY"):
                return 0

        print("[5/5] training classifier")
        models = [train(F_tr, y_tr, F_va, y_va, epochs=args.epochs, seed=sd, pos_boost=args.pos_boost)[0] for sd in range(args.seeds)]
        export_onnx(models, out_path)

        # sanity: ONNX == torch on a few samples
        import onnxruntime as ort, torch
        sess = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
        with torch.no_grad():
            ref = np.mean([torch.sigmoid(m(torch.tensor(F_va[:8]))).numpy() for m in models], axis=0)
        got = sess.run(None, {"x": F_va[:8]})[0]
        assert np.allclose(ref, got, atol=1e-4), (ref, got)
        print(f"    exported {out_path} ({out_path.stat().st_size // 1000} kB), onnx matches torch")
    else:
        aug = Augmenter(noises, babble_pool=[], seed=0)

    results = evaluate(out_path, phrase, Augmenter(noises, babble_pool=[], seed=123), args.threshold)
    (data_dir / "eval.json").write_text(json.dumps(results, indent=2))
    print("\nRESULT:", results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
