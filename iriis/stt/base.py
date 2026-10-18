"""Speech-to-text interface."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

# Things Whisper-family models say on silence/noise. Matched against the whole (normalised) utterance.
HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "thank you so much for watching",
    "please subscribe", "subscribe", "like and subscribe", "thanks for listening", "thank you for listening",
    "you", "bye", "bye bye", "okay", "so", "the end", "music", "applause", "silence", "foreign",
    "subtitles by the amara org community", "transcribed by", "see you next time", "i'm sorry",
}
_BRACKETED = re.compile(r"^[\[(♪*].*[\])♪*]$")


def is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t or _BRACKETED.match(t):
        return True
    norm = re.sub(r"[^a-z' ]", "", t.lower()).strip()
    if norm in HALLUCINATIONS:
        return True
    words = norm.split()
    # the same word or short phrase repeated many times
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    return False


@dataclass
class Transcript:
    text: str
    duration_s: float
    elapsed_s: float
    confidence: float | None = None

    @property
    def rtf(self) -> float:
        return self.elapsed_s / self.duration_s if self.duration_s else 0.0


class STTEngine(ABC):
    name: str = "stt"

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        """audio: mono float32 in [-1, 1]."""

    def warmup(self) -> None:
        self.transcribe(np.zeros(16000, dtype=np.float32))
