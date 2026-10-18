"""Wake word (openWakeWord, ONNX) and push-to-talk triggers."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from iriis.config import ROOT, WakeWordConfig

OWW_DIR = ROOT / "models" / "wakeword"
PRETRAINED = {"hey_jarvis": "hey_jarvis_v0.1.onnx"}


class WakeWord:
    def __init__(self, cfg: WakeWordConfig):
        from openwakeword.model import Model

        path = Path(cfg.model) if cfg.model.endswith(".onnx") else OWW_DIR / PRETRAINED.get(cfg.model, f"{cfg.model}.onnx")
        for p in (path, OWW_DIR / "melspectrogram.onnx", OWW_DIR / "embedding_model.onnx"):
            if not p.exists():
                raise FileNotFoundError(f"{p} missing. Run scripts/download_models.py --only wakeword")
        self.model = Model(
            wakeword_models=[str(path)], inference_framework="onnx",
            melspec_model_path=str(OWW_DIR / "melspectrogram.onnx"),
            embedding_model_path=str(OWW_DIR / "embedding_model.onnx"),
        )
        self.threshold = cfg.threshold
        self.last_score = 0.0

    def feed(self, block: np.ndarray) -> bool:
        """block: float32 16 kHz, ideally 1280 samples (80 ms). True when the wake word fires."""
        pcm = (np.clip(block, -1, 1) * 32767).astype(np.int16)
        scores = self.model.predict(pcm)
        self.last_score = float(max(scores.values())) if scores else 0.0
        return self.last_score >= self.threshold

    def reset(self) -> None:
        self.model.reset()


class PushToTalk:
    """Hold a key to talk (pynput global listener). `pressed` is set while the key is held."""

    def __init__(self, key: str = "f9"):
        from pynput import keyboard

        self.pressed = threading.Event()
        self.released = threading.Event()
        target = getattr(keyboard.Key, key.lower(), None) or keyboard.KeyCode.from_char(key)

        def on_press(k):
            if k == target and not self.pressed.is_set():
                self.released.clear()
                self.pressed.set()

        def on_release(k):
            if k == target:
                self.pressed.clear()
                self.released.set()

        self.listener = keyboard.Listener(on_press=on_press, on_release=on_release, daemon=True)
        self.listener.start()

    def stop(self) -> None:
        self.listener.stop()
