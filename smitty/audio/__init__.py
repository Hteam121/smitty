"""Audio I/O. Everything above this layer only sees AudioSource / AudioSink."""

from smitty.audio.base import SAMPLE_RATE, FRAME_SAMPLES, AudioSink, AudioSource

__all__ = ["SAMPLE_RATE", "FRAME_SAMPLES", "AudioSink", "AudioSource"]
