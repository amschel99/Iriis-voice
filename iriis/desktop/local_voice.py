"""A small on-device voice, so Iriis can still talk when her cloud brain is unreachable
("I'm not connected"). Loaded lazily; Kokoro first, Piper as the lighter fallback."""
from __future__ import annotations

import asyncio
import logging
import threading

from iriis.audio.playback import Player
from iriis.config import Config

log = logging.getLogger("iriis.local_voice")


class LocalVoice:
    def __init__(self, cfg: Config, player: Player | None):
        self.cfg = cfg
        self.player = player
        self._tts = None
        self._lock = threading.Lock()
        self.failed = False

    def _load(self):
        with self._lock:
            if self._tts is None and not self.failed:
                from iriis.tts import make_tts
                for backend in ("kokoro", "piper"):   # on-device only: it must work without the network
                    try:
                        self._tts = make_tts(self.cfg.tts, backend)
                        break
                    except Exception as e:  # noqa: BLE001
                        log.warning("local TTS %s unavailable: %s", backend, e)
                self.failed = self._tts is None
        return self._tts

    def _speak(self, text: str) -> None:
        from iriis.tts.text_normalize import normalize
        tts = self._load()
        if tts is None or self.player is None:
            return
        audio = tts.synthesize(normalize(text))
        self.player.play(audio, tts.sample_rate)

    async def say(self, text: str) -> None:
        if self.player is None:
            return
        try:
            await asyncio.to_thread(self._speak, text)
        except Exception as e:  # noqa: BLE001
            log.warning("local speech failed: %s", e)
