from iriis.config import TTSConfig
from iriis.tts.base import TTSEngine


def make_tts(cfg: TTSConfig, backend: str | None = None) -> TTSEngine:
    backend = backend or cfg.backend
    if backend == "piper":
        from iriis.tts.piper_engine import PiperEngine
        return PiperEngine(cfg)
    from iriis.tts.kokoro_engine import KokoroEngine
    return KokoroEngine(cfg)


__all__ = ["TTSEngine", "make_tts"]
