"""Speaker output on a dedicated thread, interruptible at any moment (for barge-in)."""
from __future__ import annotations

import queue
import threading
import time
from math import gcd

import numpy as np

from iriis.audio.devices import resolve_device


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(audio) == 0:
        return audio
    try:
        from scipy.signal import resample_poly
        g = gcd(src, dst)
        return resample_poly(audio, dst // g, src // g).astype(np.float32)
    except ImportError:
        n = int(len(audio) * dst / src)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def chime(sr: int = 24000, rising: bool = True) -> np.ndarray:
    """Two short soft tones (rising = listening, falling = done)."""
    def tone(f, ms):
        t = np.arange(int(sr * ms / 1000)) / sr
        env = np.minimum(1, np.minimum(t, t[::-1]) * 60)
        return 0.18 * np.sin(2 * np.pi * f * t) * env
    a, b = (880, 1320) if rising else (1320, 880)
    return np.concatenate([tone(a, 70), np.zeros(int(sr * 0.02)), tone(b, 90)]).astype(np.float32)


class Player:
    """Plays queued float32 clips. `stop()` cuts audio within one block (~20 ms)."""

    def __init__(self, device: int | str | None = None, sample_rate: int = 24000):
        self.device = resolve_device(device, "output")
        self.sample_rate = sample_rate
        self.q: queue.Queue[np.ndarray | None] = queue.Queue()
        self._stop_now = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self.level = 0.0                  # RMS of what is being played right now
        self._recent_levels: list[tuple[float, float]] = []   # (time, rms) of the last ~0.6 s played
        self.last_audio_end = 0.0         # time.monotonic() when the speaker last went quiet
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)
        self._thread.start()

    def play(self, audio: np.ndarray, sr: int) -> None:
        self._idle.clear()
        self.q.put(resample(audio.astype(np.float32), sr, self.sample_rate))

    @property
    def echo_level(self) -> float:
        """Loudest output in the last ~0.6 s. The mic hears the room's echo slightly late, and streamed
        speech arrives in small chunks with tiny gaps, so the instantaneous level is not enough."""
        now = time.monotonic()
        return max((l for t, l in self._recent_levels if now - t < 0.6), default=0.0)

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def stop(self) -> None:
        """Flush the queue and silence the current clip immediately."""
        self._stop_now.set()
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def _run(self) -> None:
        import sounddevice as sd

        block = int(self.sample_rate * 0.02)
        with sd.OutputStream(device=self.device, samplerate=self.sample_rate, channels=1, dtype="float32",
                             blocksize=block) as out:
            while True:
                try:
                    clip = self.q.get(timeout=0.1)
                except queue.Empty:
                    if not self._idle.is_set():
                        self.level = 0.0
                        self.last_audio_end = time.monotonic()
                        self._idle.set()
                    self._stop_now.clear()
                    continue
                if clip is None:
                    break
                self._stop_now.clear()
                for i in range(0, len(clip), block):
                    if self._stop_now.is_set():
                        break
                    chunk = clip[i:i + block]
                    self.level = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
                    now = time.monotonic()
                    self._recent_levels = [(t, l) for t, l in self._recent_levels if now - t < 0.6] + [(now, self.level)]
                    out.write(chunk.reshape(-1, 1))
                self.level = 0.0

    def close(self) -> None:
        self.stop()
        self.q.put(None)
