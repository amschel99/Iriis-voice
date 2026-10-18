"""NVIDIA Parakeet TDT 0.6B (INT8 ONNX) via sherpa-onnx. Fast on CPU, no PyTorch."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import numpy as np

from iriis.config import STTConfig, resolve_path
from iriis.stt.base import STTEngine, Transcript, is_hallucination


def _find(d: Path, stem: str) -> str:
    hits = sorted(d.glob(f"{stem}*.onnx"), key=lambda p: ("int8" not in p.name, p.name))
    if not hits:
        raise FileNotFoundError(f"{stem}*.onnx not found in {d}")
    return str(hits[0])


class ParakeetEngine(STTEngine):
    name = "parakeet"

    def __init__(self, cfg: STTConfig, model_dir: str | None = None):
        import sherpa_onnx

        d = resolve_path(model_dir or cfg.parakeet_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"Parakeet model folder missing: {d}. Run scripts/download_models.py --only parakeet")
        threads = cfg.cpu_threads or max(1, min(8, (os.cpu_count() or 4) // 2))
        kwargs = {}
        if cfg.vocabulary and (d / "bpe.vocab").exists():
            # Hotword biasing needs modified_beam_search plus the model's BPE vocab file.
            fd, hot = tempfile.mkstemp(suffix=".txt", prefix="iriis-hotwords-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(cfg.vocabulary))
            kwargs = {"decoding_method": "modified_beam_search", "hotwords_file": hot, "hotwords_score": 1.5,
                      "modeling_unit": "bpe", "bpe_vocab": str(d / "bpe.vocab")}
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=_find(d, "encoder"), decoder=_find(d, "decoder"), joiner=_find(d, "joiner"),
            tokens=str(d / "tokens.txt"), num_threads=threads, model_type="nemo_transducer", **kwargs,
        )
        self.name = f"parakeet:{d.name}"

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        start = time.perf_counter()
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sample_rate, audio.astype(np.float32))
        self.recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if is_hallucination(text):
            text = ""
        return Transcript(text, len(audio) / sample_rate, time.perf_counter() - start)
