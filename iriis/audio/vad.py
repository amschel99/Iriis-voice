"""Silero VAD (v6 ONNX, the copy bundled with faster-whisper) run frame by frame, plus an endpointer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

FRAME = 512          # samples per VAD frame at 16 kHz (32 ms)
CONTEXT = 64


def default_model_path() -> Path:
    import faster_whisper
    return Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"


class SileroVAD:
    def __init__(self, model_path: str | Path | None = None):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = ort.InferenceSession(str(model_path or default_model_path()), providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)

    def frame_prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.context, frame])[None, :].astype(np.float32)
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-CONTEXT:]
        return float(out.reshape(-1)[0])

    def process(self, audio: np.ndarray) -> list[float]:
        """Feed any-length float32 audio; returns one probability per complete 512-sample frame."""
        buf = np.concatenate([self._pending, audio.astype(np.float32)])
        n = len(buf) // FRAME
        probs = [self.frame_prob(buf[i * FRAME:(i + 1) * FRAME]) for i in range(n)]
        self._pending = buf[n * FRAME:]
        return probs


@dataclass
class EndpointEvent:
    kind: str                # "speech_start" | "speech_end" | "timeout"
    audio: np.ndarray | None = None


class Endpointer:
    """Collects one utterance: waits for speech, records it, ends after `silence_ms` of quiet."""

    def __init__(self, vad: SileroVAD, threshold: float = 0.5, silence_ms: int = 700, min_speech_ms: int = 250,
                 max_utterance_s: float = 20.0, no_speech_timeout_s: float = 6.0, preroll_ms: int = 300, sr: int = 16000):
        self.vad, self.threshold, self.sr = vad, threshold, sr
        self.silence_frames = int(silence_ms / 1000 * sr / FRAME)
        self.min_speech_frames = max(1, int(min_speech_ms / 1000 * sr / FRAME))
        self.max_samples = int(max_utterance_s * sr)
        self.no_speech_frames = int(no_speech_timeout_s * sr / FRAME)
        self.preroll = int(preroll_ms / 1000 * sr)
        self.reset()

    def reset(self) -> None:
        self.vad.reset()
        self.ring = np.zeros(0, dtype=np.float32)
        self.chunks: list[np.ndarray] = []
        self.in_speech = False
        self.speech_frames = 0
        self.quiet_frames = 0
        self.frames_waiting = 0
        self._started = False

    def feed(self, audio: np.ndarray) -> EndpointEvent | None:
        probs = self.vad.process(audio)
        event = None
        if self.in_speech:
            self.chunks.append(audio)
        else:
            self.ring = np.concatenate([self.ring, audio])[-self.preroll:]
        for p in probs:
            if p >= self.threshold:
                self.speech_frames += 1
                self.quiet_frames = 0
                if not self.in_speech and self.speech_frames >= 2:
                    self.in_speech = True
                    self.chunks = [self.ring.copy()]
                    if not self._started:
                        self._started = True
                        event = EndpointEvent("speech_start")
            else:
                if self.in_speech:
                    self.quiet_frames += 1
                    # threshold hysteresis: weak frames don't count as speech
                    if self.quiet_frames >= self.silence_frames:
                        return self._finish()
                else:
                    self.speech_frames = max(0, self.speech_frames - 1)
                    self.frames_waiting += 1
                    if not self._started and self.frames_waiting >= self.no_speech_frames:
                        return EndpointEvent("timeout")
        if self.in_speech and sum(len(c) for c in self.chunks) >= self.max_samples:
            return self._finish()
        return event

    def _finish(self) -> EndpointEvent:
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)
        enough = self.speech_frames >= self.min_speech_frames
        self.reset()
        return EndpointEvent("speech_end", audio if enough else None)
