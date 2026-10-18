"""Check that the offline fallback really works with no network, and report peak RAM.

    python scripts/verify_offline.py            # loads every local model with HF offline, reports RAM
    python scripts/verify_offline.py --llm      # also asks the local Ollama model a question

For a true unplugged test: disconnect Wi-Fi/Ethernet, then run this and `python -m iriis --offline`.
The script also blocks outbound sockets in-process (except localhost), so any hidden
download attempt fails loudly instead of silently succeeding.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

_real_connect = socket.socket.connect


def _local_only(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OSError(f"BLOCKED outbound connection to {address} (offline check)")
    return _real_connect(self, address)


socket.socket.connect = _local_only


def rss_gb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / 1024**3


def step(name, fn):
    t = time.perf_counter()
    try:
        out = fn()
        print(f"  ok    {name:<38} {time.perf_counter() - t:5.1f} s   RSS {rss_gb():.2f} GB")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {name:<38} {e.__class__.__name__}: {e}")
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--llm", action="store_true")
    args = p.parse_args()
    import numpy as np
    from iriis.config import load_config

    cfg = load_config()
    print("Loading every local model with networking blocked:")
    from iriis.audio.vad import SileroVAD
    step("Silero VAD", SileroVAD)
    from iriis.audio.wakeword import WakeWord
    step("openWakeWord hey_jarvis", lambda: WakeWord(cfg.wakeword))
    from iriis.stt import make_stt
    stt = step(f"STT {cfg.stt.backend}", lambda: make_stt(cfg.stt))
    if stt:
        step("STT transcribe 1 s of silence", lambda: stt.transcribe(np.zeros(16000, dtype=np.float32)))
    from iriis.tts import make_tts
    tts = step("TTS kokoro", lambda: make_tts(cfg.tts))
    if tts:
        step("TTS synthesize", lambda: tts.synthesize("Offline check complete."))
    step("TTS piper", lambda: make_tts(cfg.tts, "piper").synthesize("Hello."))
    if args.llm:
        import httpx
        def ask():
            r = httpx.post(cfg.llm.host + "/api/chat", timeout=600, json={
                "model": cfg.llm.offline_model, "stream": False, "messages": [{"role": "user", "content": "Say OK."}]})
            return r.json()["message"]["content"]
        print("  LLM says:", step(f"Ollama {cfg.llm.offline_model}", ask))
    import psutil
    print(f"\nIriis process RSS: {rss_gb():.2f} GB (the Ollama model is a separate process: see `ollama ps`).")
    print(f"System RAM free: {psutil.virtual_memory().available / 1024**3:.1f} GB")


if __name__ == "__main__":
    main()
