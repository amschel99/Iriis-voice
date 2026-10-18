"""Text-to-speech interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class TTSEngine(ABC):
    name: str = "tts"
    sample_rate: int = 24000

    @abstractmethod
    def synthesize(self, text: str) -> np.ndarray:
        """Return mono float32 audio at self.sample_rate."""

    def warmup(self) -> None:
        self.synthesize("Ready.")
