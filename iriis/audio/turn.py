"""Smart end-of-turn detection: has the user FINISHED their thought, or just paused?

Pipecat's Smart Turn v3 (BSD-2-Clause, 8 MB ONNX, Whisper-tiny encoder) listens to the audio itself,
intonation and rhythm, not just silence. Features match transformers' WhisperFeatureExtractor
(chunk_length=8, do_normalize, right-padded to 8 s), computed with faster-whisper's extractor so the
desktop needs no `transformers`.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from iriis.config import ROOT

log = logging.getLogger("iriis.turn")
MODEL = ROOT / "models" / "turn" / "smart-turn-v3.2-cpu.onnx"
URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx"
SR = 16000
SECONDS = 8


def whisper_features(audio: np.ndarray) -> np.ndarray:
    """(1, 80, 800) log-mel features exactly like WhisperFeatureExtractor(chunk_length=8) with
    do_normalize=True, padding='max_length' (right), truncation to the LAST 8 s."""
    from faster_whisper.feature_extractor import FeatureExtractor

    x = audio.astype(np.float32)[-SECONDS * SR:]
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)            # HF do_normalize (over the real samples)
    padded = np.zeros(SECONDS * SR, dtype=np.float32)
    padded[: len(x)] = x
    fe = FeatureExtractor(feature_size=80, sampling_rate=SR, hop_length=160, chunk_length=SECONDS, n_fft=400)
    feats = fe(padded, padding=0)
    return feats[:, : SECONDS * SR // 160][None].astype(np.float32)


class SmartTurn:
    def __init__(self, path: str | Path = MODEL):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 2
        so.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def probability_complete(self, audio: np.ndarray) -> float:
        out = self.session.run(None, {self.input: whisper_features(audio)})
        return float(np.asarray(out[0]).reshape(-1)[0])


def load_smart_turn(enabled: bool) -> SmartTurn | None:
    if not enabled:
        return None
    if not MODEL.exists():
        log.warning("smart turn model missing (%s); run scripts/download_models.py --only turn", MODEL)
        return None
    try:
        return SmartTurn()
    except Exception as e:  # noqa: BLE001
        log.warning("smart turn unavailable: %s", e)
        return None
