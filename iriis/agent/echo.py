"""Recognise Iriis' own voice coming back through the microphone, by what was said.

Loudness tricks (the echo gate) catch most of it, but laptop speakers + a built-in mic leak. The brain
knows exactly what she said in the last couple of minutes, so a transcript that matches it is her own
echo, not the user: it is dropped instead of being answered.
"""
from __future__ import annotations

import re
import time
from collections import deque
from difflib import SequenceMatcher

WINDOW_S = 20.0   # echo arrives while she talks or right after; later repeats are the user quoting her


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


class SelfEcho:
    def __init__(self, window_s: float = WINDOW_S):
        self.window_s = window_s
        self.spoken: deque[tuple[float, list[str]]] = deque(maxlen=200)

    def said(self, text: str, at: float | None = None) -> None:
        """Record words she spoke; `at` = when that audio finished playing (default now)."""
        w = _words(text)
        if w:
            self.spoken.append((at or time.time(), w))

    def is_echo(self, transcript: str) -> bool:
        heard = _words(transcript)
        if len(heard) < 2:
            return False
        cutoff = time.time() - self.window_s
        recent = [w for t, w in self.spoken if t >= cutoff]
        if not recent:
            return False
        said = [x for w in recent for x in w]
        # 1) most of what we heard is a run of her recent words (order-aware fuzzy match)
        m = SequenceMatcher(None, said, heard, autojunk=False)
        matched = sum(b.size for b in m.get_matching_blocks() if b.size >= 2)   # runs of 2+ words, not stray hits
        if len(heard) >= 3 and matched / len(heard) >= 0.7:
            return True
        # 2) or nearly every word heard is one she just used (echo transcribed out of order / partially)
        vocab = set(said)
        return len(heard) >= 6 and sum(1 for w in heard if w in vocab) / len(heard) >= 0.9
