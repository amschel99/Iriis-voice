"""Orpheus (Canopy Labs) expressive TTS, served by Groq: real emotion through vocal directions.

Text may contain directions in square brackets, e.g. "[angry] Close the laptop." or "[whisper] I love you."
Groq limits each request to 200 characters, so longer text is split at sentence/comma boundaries.
"""
from __future__ import annotations

import io
import os
import re

import numpy as np

from iriis.config import TTSConfig
from iriis.tts.base import TTSEngine

URL = "https://api.groq.com/openai/v1/audio/speech"
MAX_CHARS = 200
VOICES = {"autumn", "diana", "hannah", "austin", "daniel", "troy"}   # first three are female
_DIRECTION = re.compile(r"\[[^\]]{1,40}\]")


def split_for_orpheus(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split into <= limit-char pieces; a direction like [angry] carries over to the pieces after it."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    current_dir = ""
    for part in parts:
        dirs = _DIRECTION.findall(part)
        if dirs:
            current_dir = dirs[-1]
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

        self.key = os.environ.get(cfg.orpheus_api_key_env, "")
        if not self.key:
            raise RuntimeError(f"Orpheus needs {cfg.orpheus_api_key_env} (a Groq API key)")
        self.cfg = cfg
        self.voice = cfg.orpheus_voice if cfg.orpheus_voice in VOICES else "hannah"
        self.http = httpx.Client(timeout=httpx.Timeout(30, connect=10))
        self.name = f"orpheus:{self.voice}"

    def _one(self, text: str, voice: str) -> tuple[np.ndarray, int]:
        import soundfile as sf
        r = self.http.post(URL, headers={"Authorization": f"Bearer {self.key}"},
                           json={"model": self.cfg.orpheus_model, "voice": voice, "input": text, "response_format": "wav"})
        if r.status_code != 200:
            raise RuntimeError(f"Orpheus TTS error {r.status_code}: {r.text[:200]}")
        audio, sr = sf.read(io.BytesIO(r.content), dtype="float32", always_2d=True)
        return audio.mean(axis=1), sr

    def synthesize(self, text: str, voice: str | None = None) -> np.ndarray:
        voice = voice if voice in VOICES else self.voice
        chunks, gap = [], None
        for piece in split_for_orpheus(text):
            if not re.search(r"\w", _DIRECTION.sub("", piece)):
                continue   # a bare direction with no words
            audio, sr = self._one(piece, voice)
            self.sample_rate = sr
            if gap is None:
                gap = np.zeros(int(sr * 0.12), dtype=np.float32)
            chunks += [audio, gap]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return (np.concatenate(chunks[:-1]) * self.cfg.volume).clip(-1, 1).astype(np.float32)
