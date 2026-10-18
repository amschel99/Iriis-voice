"""Kokoro-82M via kokoro-onnx (ONNX Runtime, no PyTorch)."""
from __future__ import annotations

import numpy as np

from iriis.config import TTSConfig, resolve_path
from iriis.tts.base import TTSEngine


class KokoroEngine(TTSEngine):
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, cfg: TTSConfig):
        from kokoro_onnx import Kokoro

        model, voices = resolve_path(cfg.kokoro_model), resolve_path(cfg.kokoro_voices)
        for p in (model, voices):
            if not p.exists():
                raise FileNotFoundError(f"{p} missing. Run scripts/download_models.py --only tts")
        self.kokoro = Kokoro(str(model), str(voices))
        self.cfg = cfg
        available = set(self.kokoro.get_voices())
        if cfg.voice not in available:
            raise ValueError(f"Unknown Kokoro voice {cfg.voice!r}. Available: {', '.join(sorted(available))}")
        # Voice prefixes: a = American, b = British English.
        self.lang = "en-gb" if cfg.voice.startswith("b") else "en-us"
        self.name = f"kokoro:{cfg.voice}"

    def synthesize(self, text: str) -> np.ndarray:
        samples, sr = self.kokoro.create(text, voice=self.cfg.voice, speed=self.cfg.speed, lang=self.lang)
        self.sample_rate = sr
        return (np.asarray(samples, dtype=np.float32) * self.cfg.volume).clip(-1, 1)
