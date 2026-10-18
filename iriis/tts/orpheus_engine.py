"""Orpheus expressive TTS, two flavours behind one engine:

  groq  hosted orpheus-v1 on Groq. Voices hannah/diana/autumn/troy/austin/daniel; [any direction]; 200 chars/request.
  open  self-hosted canopylabs/orpheus-3b-0.1-ft (deploy/orpheus_modal.py on a Modal GPU). Voices tara/leah/jess/
        mia/zoe/leo/dan/zac; <laugh> <sigh>... sound tags; no rate limits. Scales to zero, so the first request
        after a quiet spell waits for a GPU: warm() wakes it early and the server speaks with Kokoro meanwhile.

Both speak the same HTTP API (POST .../audio/speech -> WAV), so the flavour mostly changes text and voices.
"""
from __future__ import annotations

import io
import os
import re
import threading
import time

import numpy as np

from iriis.config import TTSConfig
from iriis.tts.base import TTSEngine
from iriis.tts.text_normalize import DIRECTION_RE, GROQ_VOICES, OPEN_VOICES, SOUND_TAG_RE, voice_for

GROQ_URL = "https://api.groq.com/openai/v1/audio/speech"
MAX_CHARS = 200            # Groq's per-request limit (the open server takes ~1200)
OPEN_MAX_CHARS = 400
WARM_FOR_S = 240           # the GPU host stays up >= 300 s after the last request
WARM_POLL_S = 10
_TAG = re.compile(r"\[[^\]]{1,40}\]|<[a-z]{3,8}>")


def split_for_orpheus(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split into <= limit-char pieces; a [direction] carries over to the pieces after it."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    current_dir = ""
    for part in parts:
        dirs = DIRECTION_RE.findall(part)
        if dirs:
            current_dir = f"[{dirs[-1]}]"
        while part:
            carry = current_dir and not part.startswith("[")
            room = limit - (len(current_dir) + 1 if carry else 0)   # leave space to repeat the direction
            if len(part) <= room:
                piece, part = part, ""
            else:
                cut = max(part.rfind(", ", 0, room), part.rfind(" ", 0, room))
                cut = cut if cut > 20 else room
                piece, part = part[:cut + 1], part[cut + 1:].strip()
            piece = piece.strip()
            if piece and carry:
                piece = f"{current_dir} {piece}"     # keep the emotion going across pieces
            if piece:
                out.append(piece)
    return out


class OrpheusEngine(TTSEngine):
    name = "orpheus"
    sample_rate = 24000

    def __init__(self, cfg: TTSConfig):
        import httpx

        self.cfg = cfg
        self.flavor = cfg.orpheus_flavor
        if self.flavor == "open":
            if not cfg.orpheus_url:
                raise RuntimeError("tts.orpheus_url is not set (your voice server's URL)")
            self.url = cfg.orpheus_url.rstrip("/") + "/v1/audio/speech"
            self.health_url = cfg.orpheus_url.rstrip("/") + "/health"
            key_env, self.pool, self.limit = cfg.orpheus_token_env, OPEN_VOICES, OPEN_MAX_CHARS
            self.model = "canopylabs/orpheus-3b-0.1-ft"
        else:
            self.url, self.health_url = cfg.orpheus_url or GROQ_URL, None
            key_env, self.pool, self.limit = cfg.orpheus_api_key_env, GROQ_VOICES, MAX_CHARS
            self.model = cfg.orpheus_model
        self.key = os.environ.get(key_env, "")
        if not self.key:
            raise RuntimeError(f"Orpheus ({self.flavor}) needs {key_env}")
        # A host with its own auth (e.g. a *protected* Hugging Face endpoint) takes its token in Authorization;
        # then our ORPHEUS_TOKEN travels in X-Orpheus-Token instead.
        host_token = os.environ.get(cfg.orpheus_host_token_env, "") if cfg.orpheus_host_token_env else ""
        if self.flavor == "open" and host_token:
            self.headers = {"Authorization": f"Bearer {host_token}", "X-Orpheus-Token": self.key}
        elif self.flavor == "open":
            # Hugging Face Endpoints strip Authorization before it reaches the container: send both.
            self.headers = {"Authorization": f"Bearer {self.key}", "X-Orpheus-Token": self.key}
        else:
            self.headers = {"Authorization": f"Bearer {self.key}"}
        self.voice = voice_for(cfg.orpheus_voice, self.flavor) or self.pool[0]
        self.http = httpx.Client(timeout=httpx.Timeout(60, connect=10))
        self.name = f"orpheus-{self.flavor}:{self.voice}"
        self.style = self.flavor          # text style for iriis.tts.text_normalize.normalize
        self._warm_until = float("inf") if self.flavor == "groq" else 0.0
        self._warming: threading.Thread | None = None

    # ------------------------------------------------------------------ warm-up (open flavour)

    @property
    def warm(self) -> bool:
        return time.time() < self._warm_until

    def ensure_warm(self) -> bool:
        """True if the GPU can speak right now. Our "warm" flag may have expired while the host is
        still up (it stays up 15 min after the last request), so ask it quickly before giving up;
        if it really is asleep, start waking it and let the caller use the fallback voice."""
        if self.warm:
            return True
        if self.health_url is None:
            return True
        try:
            r = self.http.get(self.health_url, headers=self.headers, timeout=2.5)
            if r.status_code == 200:
                self._warm_until = time.time() + WARM_FOR_S
                return True
        except Exception:  # noqa: BLE001
            pass
        self.warm_up()
        return False

    def warm_up(self, wait: bool = False) -> None:
        """Wake the GPU container in the background (its /health answers once the model is loaded)."""
        if self.warm or self.health_url is None:
            return
        if self._warming is None or not self._warming.is_alive():
            def ping():
                # A sleeping host answers 502/503 (or times out) while it boots and loads the model:
                # keep asking for up to 15 minutes.
                deadline = time.time() + 900
                while time.time() < deadline:
                    try:
                        r = self.http.get(self.health_url, headers=self.headers, timeout=60)
                        if r.status_code == 200:
                            self._warm_until = time.time() + WARM_FOR_S
                            return
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(WARM_POLL_S)
            self._warming = threading.Thread(target=ping, name="orpheus-warmup", daemon=True)
            self._warming.start()
        if wait:
            self._warming.join()

    # ------------------------------------------------------------------ synthesis

    def _one(self, text: str, voice: str) -> tuple[np.ndarray, int]:
        import soundfile as sf
        r = self.http.post(self.url, headers=self.headers,
                           json={"model": self.model, "voice": voice, "input": text, "response_format": "wav"})
        if r.status_code != 200:
            raise RuntimeError(f"Orpheus TTS error {r.status_code}: {r.text[:200]}")
        audio, sr = sf.read(io.BytesIO(r.content), dtype="float32", always_2d=True)
        if self.flavor == "open":
            self._warm_until = time.time() + WARM_FOR_S
        return audio.mean(axis=1), sr

    def stream(self, text: str, voice: str | None = None, stop=None):
        """Yield float32 audio chunks while the GPU is still generating (open flavour: /stream endpoint).
        `stop` is an optional threading.Event to abandon the request (barge-in). Groq: one whole chunk."""
        if not self.ensure_warm():
            raise RuntimeError("the Orpheus GPU is waking up")   # caller speaks this one with Kokoro
        if self.flavor != "open":
            yield self.synthesize(text, voice)
            return
        voice = voice_for(voice, self.flavor) or self.voice
        first = True
        for piece in split_for_orpheus(text, self.limit):
            if not re.search(r"\w", _TAG.sub("", SOUND_TAG_RE.sub("", piece))):
                continue
            if not first:
                yield np.zeros(int(self.sample_rate * 0.12), dtype=np.float32)
            first = False
            with self.http.stream("POST", self.url + "/stream", headers=self.headers,
                                  json={"model": self.model, "voice": voice, "input": piece}) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"Orpheus stream error {r.status_code}: {r.read()[:200]!r}")
                self.sample_rate = int(r.headers.get("x-sample-rate", self.sample_rate))
                buf = b""
                for data in r.iter_bytes():
                    if stop is not None and stop.is_set():
                        return
                    buf += data
                    n = len(buf) // 2 * 2
                    if n:
                        pcm = np.frombuffer(buf[:n], dtype="<i2").astype(np.float32) / 32768.0
                        buf = buf[n:]
                        yield (pcm * self.cfg.volume).clip(-1, 1)
            self._warm_until = time.time() + WARM_FOR_S

    def synthesize(self, text: str, voice: str | None = None) -> np.ndarray:
        """`text` should already be normalised with style=self.style."""
        if not self.ensure_warm():
            raise RuntimeError("the Orpheus GPU is waking up")   # caller speaks this one with Kokoro
        voice = voice_for(voice, self.flavor) or self.voice
        chunks, gap = [], None
        for piece in split_for_orpheus(text, self.limit):
            if not re.search(r"\w", _TAG.sub("", SOUND_TAG_RE.sub("", piece))):
                continue   # a bare tag with no words
            audio, sr = self._one(piece, voice)
            self.sample_rate = sr
            if gap is None:
                gap = np.zeros(int(sr * 0.12), dtype=np.float32)
            chunks += [audio, gap]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return (np.concatenate(chunks[:-1]) * self.cfg.volume).clip(-1, 1).astype(np.float32)
