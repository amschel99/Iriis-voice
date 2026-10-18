"""faster-whisper (CTranslate2) backend, for AfriSpeech fine-tuned Whisper models."""
from __future__ import annotations

import time

import numpy as np

from iriis.config import STTConfig, resolve_path
from iriis.stt.base import STTEngine, Transcript, is_hallucination


def cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class FasterWhisperEngine(STTEngine):
    name = "faster_whisper"

    def __init__(self, cfg: STTConfig, model: str | None = None):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        path = resolve_path(model or cfg.model)
        if not (path / "model.bin").exists():
            raise FileNotFoundError(f"No CTranslate2 model at {path}. Run scripts/convert_models.py first.")
        device = cfg.device if cfg.device != "auto" else ("cuda" if cuda_available() else "cpu")
        compute = cfg.compute_type if cfg.compute_type != "auto" else ("float16" if device == "cuda" else "int8")
        self.device, self.compute_type = device, compute
        self.model = WhisperModel(str(path), device=device, compute_type=compute, cpu_threads=cfg.cpu_threads,
                                  local_files_only=True)
        self.name = f"faster_whisper:{path.name}"
        self.prompt = ("Vocabulary: " + ", ".join(cfg.vocabulary) + ".") if cfg.vocabulary and cfg.use_vocabulary_prompt else None

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcript:
        assert sample_rate == 16000, "faster-whisper expects 16 kHz audio"
        start = time.perf_counter()
        segments, info = self.model.transcribe(
            audio.astype(np.float32),
            language="en",
            task="transcribe",
            beam_size=self.cfg.beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            initial_prompt=self.prompt,
            condition_on_previous_text=False,
            without_timestamps=True,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            temperature=[0.0, 0.2, 0.4],
        )
        texts, probs = [], []
        for seg in segments:
            if seg.no_speech_prob > 0.8 and seg.avg_logprob < -0.8:
                continue
            texts.append(seg.text.strip())
            probs.append(float(np.exp(seg.avg_logprob)))
        text = " ".join(t for t in texts if t).strip()
        if self.prompt and text.strip(" .") == self.prompt.strip(" ."):
            text = ""  # echoed the vocabulary prompt
        if is_hallucination(text):
            text = ""
        return Transcript(text, len(audio) / sample_rate, time.perf_counter() - start,
                          float(np.mean(probs)) if probs else None)
