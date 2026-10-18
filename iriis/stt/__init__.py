from iriis.config import STTConfig
from iriis.stt.base import STTEngine, Transcript


def make_stt(cfg: STTConfig, backend: str | None = None, model: str | None = None) -> STTEngine:
    backend = backend or cfg.backend
    if backend == "parakeet":
        from iriis.stt.sherpa_parakeet_engine import ParakeetEngine
        return ParakeetEngine(cfg, model)
    from iriis.stt.faster_whisper_engine import FasterWhisperEngine
    return FasterWhisperEngine(cfg, model)


__all__ = ["STTEngine", "Transcript", "make_stt"]
