"""Acoustic echo cancellation (WebRTC AEC3 via LiveKit's AudioProcessingModule), like a phone call:
the speaker tells it what she is playing ("far end"); the mic signal ("near end") has that removed,
so she can listen to you while she talks and doesn't hear herself. Measured: 26-34 dB of echo removed.

Both streams are 16 kHz mono, 10 ms frames (160 samples). Thread-safe: the player thread feeds the far
end, PortAudio's capture thread cleans the near end.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger("iriis.aec")
SR = 16000
FRAME = 160   # 10 ms


def _to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


class EchoCanceller:
    def __init__(self, noise_suppression: bool = True, delay_ms: int = 60):
        from livekit import rtc

        self._rtc = rtc
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=noise_suppression,
                                             high_pass_filter=True, auto_gain_control=False)
        self.apm.set_stream_delay_ms(delay_ms)
        self._lock = threading.Lock()
        self._far = np.zeros(0, dtype=np.float32)
        self._near = np.zeros(0, dtype=np.float32)

    def feed_far(self, audio: np.ndarray, sample_rate: int) -> None:
        """What the speaker is playing right now (any sample rate)."""
        from iriis.audio.playback import resample
        x = resample(audio.astype(np.float32), sample_rate, SR) if sample_rate != SR else audio.astype(np.float32)
        with self._lock:
            buf = np.concatenate([self._far, x])
            n = len(buf) // FRAME
            for i in range(n):
                f = self._rtc.AudioFrame(_to_pcm(buf[i * FRAME:(i + 1) * FRAME]), SR, 1, FRAME)
                self.apm.process_reverse_stream(f)
            self._far = buf[n * FRAME:]

    def clean(self, block: np.ndarray) -> np.ndarray:
        """Mic audio (16 kHz) in, echo-cancelled mic audio out (same length, ~10 ms of extra latency)."""
        with self._lock:
            buf = np.concatenate([self._near, block.astype(np.float32)])
            n = len(buf) // FRAME
            out = np.empty(n * FRAME, dtype=np.float32)
            for i in range(n):
                f = self._rtc.AudioFrame(_to_pcm(buf[i * FRAME:(i + 1) * FRAME]), SR, 1, FRAME)
                self.apm.process_stream(f)
                out[i * FRAME:(i + 1) * FRAME] = np.frombuffer(bytes(f.data), dtype="<i2").astype(np.float32) / 32768.0
            self._near = buf[n * FRAME:]
        if len(out) < len(block):    # keep block sizes stable for the wake word (pad the 10 ms remainder)
            out = np.concatenate([np.zeros(len(block) - len(out), dtype=np.float32), out])
        return out[-len(block):]


def make_echo_canceller(enabled: bool, noise_suppression: bool = True) -> EchoCanceller | None:
    if not enabled:
        return None
    try:
        return EchoCanceller(noise_suppression=noise_suppression)
    except Exception as e:  # noqa: BLE001  (livekit not installed / unsupported platform)
        log.warning("echo cancellation unavailable (%s); using the echo gate only", e)
        return None
