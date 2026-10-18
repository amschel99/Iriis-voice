"""Latency benchmark against a running Iriis (local or Railway) over the voice WebSocket.

    python scripts/benchmark_latency.py --server wss://your-app.up.railway.app --audio-dir benchmarks/my_voice
    python scripts/benchmark_latency.py --server ws://127.0.0.1:8000 --text "what time is it" --runs 5

Per request it measures: end of speech -> transcript (STT, network included),
transcript -> first LLM token, end of speech -> first audio, and the total. Prints a table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def one(ws, audio: np.ndarray | None, text: str | None) -> dict[str, float]:
    t0 = time.perf_counter()
    if audio is not None:
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        await ws.send(json.dumps({"type": "start_utterance"}))
        for i in range(0, len(pcm), 32000):
            await ws.send(pcm[i:i + 32000])
        t0 = time.perf_counter()   # "end of speech" = the moment the utterance is complete
        await ws.send(json.dumps({"type": "end_utterance"}))
    else:
        await ws.send(json.dumps({"type": "text", "text": text}))
    out: dict[str, float] = {}
    while True:
        msg = await asyncio.wait_for(ws.recv(), 120)
        now = (time.perf_counter() - t0) * 1000
        if isinstance(msg, bytes):
            out.setdefault("first_audio_ms", now)
            continue
        ev = json.loads(msg)
        if ev["type"] == "transcript":
            out["transcript_ms"] = now
            if not ev.get("text"):
                return out
        elif ev["type"] == "sentence":
            out.setdefault("first_sentence_ms", now)
        elif ev["type"] in ("done", "error"):
            out["total_ms"] = now
            server = ev.get("timings", {})
            if "first_token_ms" in server:
                out["llm_first_token_ms"] = server["first_token_ms"]
            return out


async def main() -> None:
    from websockets.asyncio.client import connect
    import soundfile as sf

    p = argparse.ArgumentParser()
    p.add_argument("--server", default="ws://127.0.0.1:8000")
    p.add_argument("--audio-dir")
    p.add_argument("--text", default="What's fifteen percent of two thousand four hundred?")
    p.add_argument("--runs", type=int, default=5)
    args = p.parse_args()
    key = os.environ.get("IRIIS_API_KEY", "")
    inputs: list[tuple[np.ndarray | None, str | None]] = []
    if args.audio_dir:
        for wav in sorted(Path(args.audio_dir).glob("*.wav"))[: args.runs]:
            a, sr = sf.read(wav, dtype="float32")
            inputs.append((a if a.ndim == 1 else a.mean(axis=1), None))
    else:
        inputs = [(None, args.text)] * args.runs
    rows = []
    async with connect(args.server.rstrip("/") + "/v1/voice", additional_headers={"Authorization": f"Bearer {key}"} if key else {},
                       max_size=2**25) as ws:
        await ws.send(json.dumps({"type": "hello", "session_id": "latency-bench", "speak": True}))
        await ws.recv()
        for audio, text in inputs:
            await ws.send(json.dumps({"type": "reset"}))
            await ws.recv()
            r = await one(ws, audio, text)
            rows.append(r)
            print({k: round(v) for k, v in r.items()})
    from rich.console import Console
    from rich.table import Table
    keys = ["transcript_ms", "llm_first_token_ms", "first_sentence_ms", "first_audio_ms", "total_ms"]
    t = Table(title=f"Latency over {len(rows)} runs (ms)")
    t.add_column("stage")
    for c in ("median", "p90", "min"):
        t.add_column(c, justify="right")
    for k in keys:
        vals = sorted(r[k] for r in rows if k in r)
        if vals:
            t.add_row(k, f"{statistics.median(vals):.0f}", f"{vals[int(0.9 * (len(vals) - 1))]:.0f}", f"{vals[0]:.0f}")
    Console().print(t)


if __name__ == "__main__":
    asyncio.run(main())
