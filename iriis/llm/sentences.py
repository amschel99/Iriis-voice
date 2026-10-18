"""Split a token stream into speakable sentences as early as possible."""
from __future__ import annotations

import re

_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "jr", "sr", "inc", "ltd"}
_END = re.compile(r"([.!?]+[\"')\]]*|\n{2,}|:\n|\n(?=\s*(?:[-*•]|\d+[.)])\s))(\s+|$)")


class SentenceChunker:
    """Feed tokens with push(); get complete sentences back. flush() returns the remainder.

    The first chunk is emitted early (at a comma, after min_first_chars) to cut time-to-first-audio.
    """

    def __init__(self, min_chars: int = 12, first_chunk_chars: int = 60):
        self.buf = ""
        self.min_chars = min_chars
        self.first_chunk_chars = first_chunk_chars
        self.emitted = 0
        self._in_code = False

    def push(self, token: str) -> list[str]:
        self.buf += token
        out: list[str] = []
        while True:
            sent = self._take()
            if sent is None:
                break
            if sent.strip():
                out.append(sent.strip())
                self.emitted += 1
        return out

    def _take(self) -> str | None:
        # Hold everything inside a fenced code block; it is replaced by "on screen" later.
        if self.buf.count("```") % 2 == 1:
            return None
        for m in _END.finditer(self.buf):
            end = m.end(1)
            candidate = self.buf[:end]
            if m.group(2) == "" and m.end() == len(self.buf):
                return None  # might be "3." of "3.5" or more "!!!" coming
            words = candidate.rstrip(".!?\"') ]").split()
            last = words[-1].lower() if words else ""
            if last in _ABBREV or re.fullmatch(r"[a-z]", last):
                continue
            if len(candidate.strip()) < self.min_chars and m.group(1)[0] in ".":
                continue
            self.buf = self.buf[m.end():]
            return candidate
        if self.emitted == 0 and len(self.buf) > self.first_chunk_chars:
            idx = max(self.buf.rfind(", "), self.buf.rfind("; "), self.buf.rfind(" - "))
            if idx > 20:
                candidate, self.buf = self.buf[: idx + 1], self.buf[idx + 2:]
                return candidate
        return None

    def flush(self) -> str:
        rest, self.buf = self.buf.strip(), ""
        return rest
