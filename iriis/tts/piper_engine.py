"""Piper TTS (ultra-light VITS voices, ONNX)."""
from __future__ import annotations

import numpy as np

from iriis.config import TTSConfig, resolve_path
from iriis.tts.base import TTSEngine


class PiperEngine(TTSEngine):
    name = "piper"

    def __init__(self, cfg: TTSConfig):
        from piper import PiperVoice, SynthesisConfig

        model = resolve_path(cfg.piper_model)
        if not model.exists():
            raise FileNotFoundError(f"{model} missing. Run scripts/download_models.py --only tts")
        self.voice = PiperVoice.load(str(model))
        self.sample_rate = self.voice.config.sample_rate
        # length_scale > 1 is slower speech
        self.syn = SynthesisConfig(volume=cfg.volume, length_scale=1.0 / max(cfg.speed, 0.1))
        self.name = f"piper:{model.stem}"

    def synthesize(self, text: str) -> np.ndarray:
        chunks = [c.audio_float_array for c in self.voice.synthesize(text, syn_config=self.syn)]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)
