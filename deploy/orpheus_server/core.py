"""Iriis' own Orpheus voice server: canopylabs/orpheus-3b-0.1-ft on vLLM, decoded with SNAC (24 kHz).

Host-agnostic: the same code runs in the Docker image (Hugging Face Inference Endpoints, RunPod, any GPU box)
and on Modal. API (Groq-compatible, so Iriis just changes the URL):
    GET  /health                       -> 200 once the model is loaded (503 while loading)
    POST /v1/audio/speech              {"input": "...", "voice": "tara"} -> audio/wav, 24 kHz mono
    POST /v1/audio/speech/stream       same body -> raw PCM16 LE mono 24 kHz, streamed while it is generated
Auth: X-Orpheus-Token: <ORPHEUS_TOKEN> (some hosts, e.g. Hugging Face Endpoints, strip Authorization),
      or Authorization: Bearer <ORPHEUS_TOKEN>.
Env: ORPHEUS_QUANTIZATION=fp8 (default) or "" for full bf16.

Decoding follows canopyai/Orpheus-TTS: prompt = [START_OF_HUMAN] + tokens("voice: text") + [END markers],
generated <custom_token_N> ids -> 7-token frames -> 3 SNAC codebooks -> audio (2048 samples per frame).
Streaming decodes a sliding 4-frame window and emits its 2nd frame (1 frame of left context, 2 of right),
exactly like the reference streaming decoder, then flushes the tail at the end.
"""
from __future__ import annotations

import asyncio
import hmac
import io
import os
import threading
import uuid
from typing import AsyncIterator

MODEL_ID = os.environ.get("ORPHEUS_MODEL", "canopylabs/orpheus-3b-0.1-ft")
SNAC_ID = "hubertsiuzdak/snac_24khz"
VOICES = ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"]
SAMPLE_RATE = 24000
SAMPLES_PER_FRAME = 2048

# Special tokens (Llama-3.2 vocab + Orpheus audio tokens)
START_OF_HUMAN, END_OF_TEXT, END_OF_HUMAN, START_OF_AI, START_OF_SPEECH = 128259, 128009, 128260, 128261, 128257
END_OF_SPEECH = 128258
AUDIO_TOKEN_BASE = 128256 + 10        # <custom_token_N> has id 128256 + N; audio codes start at N = 10


class CodeCollector:
    """Turns generated token ids into SNAC codes incrementally (per-position offset (index % 7) * 4096)."""

    def __init__(self):
        self.codes: list[int] = []
        self.done = False

    def add(self, token_ids) -> None:
        for tid in token_ids:
            if self.done:
                return
            if tid == END_OF_SPEECH:
                self.done = True
                return
            code = tid - AUDIO_TOKEN_BASE - (len(self.codes) % 7) * 4096
            if 0 <= code < 4096:
                self.codes.append(code)
            # anything else (start-of-speech markers etc.) is skipped without advancing the frame position

    @property
    def frames(self) -> int:
        return len(self.codes) // 7


def codes_from_token_ids(token_ids: list[int]) -> list[int]:
    c = CodeCollector()
    c.add(token_ids)
    return c.codes[: c.frames * 7]


def split_codebooks(codes: list[int]) -> tuple[list[int], list[int], list[int]]:
    c0, c1, c2 = [], [], []
    for j in range(len(codes) // 7):
        f = codes[7 * j: 7 * j + 7]
        c0.append(f[0])
        c1 += [f[1], f[4]]
        c2 += [f[2], f[3], f[5], f[6]]
    return c0, c1, c2


def stream_plan(total_frames: int, emitted: int, finished: bool) -> tuple[int, int, int, int] | None:
    """Which window to decode next: (window_start, window_end, emit_from, emit_to) in frames, or None.
    While generating, emit one frame once 2 frames of right context exist; at the end, flush the rest."""
    if not finished:
        if total_frames < emitted + 3:
            return None
        start = max(0, emitted - 1)
        return start, emitted + 3, emitted, emitted + 1
    if emitted >= total_frames:
        return None
    return max(0, emitted - 1), total_frames, emitted, total_frames


class OrpheusModel:
    def __init__(self, cache_dir: str | None = None, gpu_memory_utilization: float = 0.80):
        self.cache_dir = cache_dir
        self.gpu_memory_utilization = gpu_memory_utilization
        self.ready = False
        self.error = ""
        self._snac_lock = threading.Lock()

    def load(self) -> None:
        try:
            import torch
            from huggingface_hub import snapshot_download
            from snac import SNAC
            from transformers import AutoTokenizer
            from vllm import AsyncEngineArgs, AsyncLLMEngine

            token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            local = snapshot_download(MODEL_ID, token=token, cache_dir=self.cache_dir)
            self.tokenizer = AutoTokenizer.from_pretrained(local)
            # fp8 weights halve the memory read per token; speech generation is bandwidth-bound.
            quant = os.environ.get("ORPHEUS_QUANTIZATION", "fp8") or None
            self.engine = AsyncLLMEngine.from_engine_args(AsyncEngineArgs(
                model=local, dtype="bfloat16", max_model_len=4096, quantization=quant,
                gpu_memory_utilization=self.gpu_memory_utilization, disable_log_requests=True))
            self.snac = SNAC.from_pretrained(SNAC_ID, cache_dir=self.cache_dir).eval().to("cuda")
            self.torch = torch
            self.ready = True
        except Exception as e:  # noqa: BLE001  (reported by /health)
            self.error = f"{e.__class__.__name__}: {e}"
            raise

    def _prompt_ids(self, text: str, voice: str) -> list[int]:
        ids = self.tokenizer(f"{voice}: {text}").input_ids          # includes <|begin_of_text|>
        return [START_OF_HUMAN, *ids, END_OF_TEXT, END_OF_HUMAN, START_OF_AI, START_OF_SPEECH]

    def _decode_window(self, codes: list[int], start: int, end: int, emit_from: int, emit_to: int) -> bytes:
        """Decode frames [start, end) and return PCM16 bytes for frames [emit_from, emit_to)."""
        import numpy as np
        torch = self.torch
        window = codes[start * 7: end * 7]
        layers = [torch.tensor(c, dtype=torch.int32, device="cuda").unsqueeze(0) for c in split_codebooks(window)]
        with self._snac_lock, torch.inference_mode():
            audio = self.snac.decode(layers).squeeze().float().cpu().numpy()
        a = (emit_from - start) * SAMPLES_PER_FRAME
        b = (emit_to - start) * SAMPLES_PER_FRAME
        return (np.clip(audio[a:b], -1, 1) * 32767).astype("<i2").tobytes()

    async def stream_pcm(self, text: str, voice: str = "tara", temperature: float = 0.6, top_p: float = 0.8,
                         repetition_penalty: float = 1.3) -> AsyncIterator[bytes]:
        """PCM16 bytes as they are generated: first chunk after ~3 frames, then ~0.25 s per chunk."""
        from vllm import SamplingParams
        from vllm.inputs import TokensPrompt
        params = SamplingParams(temperature=temperature, top_p=top_p, repetition_penalty=repetition_penalty,
                                max_tokens=3600, stop_token_ids=[END_OF_SPEECH])
        collector, seen, emitted = CodeCollector(), 0, 0
        pending = b""
        request_id = uuid.uuid4().hex
        async for out in self.engine.generate(TokensPrompt(prompt_token_ids=self._prompt_ids(text, voice)), params,
                                              request_id=request_id):
            ids = out.outputs[0].token_ids          # cumulative
            collector.add(ids[seen:])
            seen = len(ids)
            while (plan := stream_plan(collector.frames, emitted, False)) is not None:
                pending += await asyncio.to_thread(self._decode_window, collector.codes, *plan)
                emitted = plan[3]
                # first chunk as soon as possible, then ~3 frames (~0.25 s) per chunk
                if emitted == 1 or len(pending) >= 3 * SAMPLES_PER_FRAME * 2:
                    yield pending
                    pending = b""
        plan = stream_plan(collector.frames, emitted, True)
        if plan is not None:
            pending += await asyncio.to_thread(self._decode_window, collector.codes, *plan)
        if pending:
            yield pending
        if collector.frames == 0:
            raise ValueError("the model produced no audio")

    async def wav(self, text: str, voice: str = "tara", **kw) -> bytes:
        import numpy as np
        import soundfile as sf
        pcm = b"".join([chunk async for chunk in self.stream_pcm(text, voice, **kw)])
        buf = io.BytesIO()
        sf.write(buf, np.frombuffer(pcm, dtype="<i2"), SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buf.getvalue()


from pydantic import BaseModel  # noqa: E402  (module level: FastAPI resolves the annotation below)


class Speech(BaseModel):
    input: str
    voice: str = "tara"
    model: str = MODEL_ID
    response_format: str = "wav"
    temperature: float = 0.6
    top_p: float = 0.8
    repetition_penalty: float = 1.3


def create_app(model: OrpheusModel, load_in_background: bool = True):
    """FastAPI app. With load_in_background, /health answers 503 until the model is ready
    (hosts like Hugging Face Endpoints poll it to know when the container is up)."""
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    secret = os.environ.get("ORPHEUS_TOKEN", "")
    web = FastAPI(title="Iriis Orpheus TTS")

    if load_in_background:
        @web.on_event("startup")
        def _start_loading():
            threading.Thread(target=model.load, name="orpheus-load", daemon=True).start()

    def check(authorization: str | None, x_orpheus_token: str | None, req: Speech | None = None) -> None:
        token = (x_orpheus_token or (authorization or "").removeprefix("Bearer ")).strip()
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            raise HTTPException(401, "unauthorised")
        if not model.ready:
            raise HTTPException(503, "the voice model is still loading")
        if req is not None:
            if req.voice not in VOICES:
                raise HTTPException(400, f"unknown voice {req.voice!r}; choose from {VOICES}")
            if not req.input.strip() or len(req.input) > 1200:
                raise HTTPException(400, "input must be 1-1200 characters")

    def sampling(req: Speech) -> dict:
        return {"temperature": req.temperature, "top_p": req.top_p, "repetition_penalty": req.repetition_penalty}

    @web.get("/health")
    def health():
        if model.ready:
            return {"ok": True, "model": MODEL_ID, "voices": VOICES, "streaming": True}
        return JSONResponse({"ok": False, "loading": not model.error, "error": model.error}, status_code=503)

    @web.post("/v1/audio/speech")
    async def speech(req: Speech, authorization: str | None = Header(default=None),
                     x_orpheus_token: str | None = Header(default=None)):
        check(authorization, x_orpheus_token, req)
        wav = await model.wav(req.input.strip(), req.voice, **sampling(req))
        return Response(wav, media_type="audio/wav")

    @web.post("/v1/audio/speech/stream")
    async def speech_stream(req: Speech, authorization: str | None = Header(default=None),
                            x_orpheus_token: str | None = Header(default=None)):
        check(authorization, x_orpheus_token, req)
        return StreamingResponse(model.stream_pcm(req.input.strip(), req.voice, **sampling(req)),
                                 media_type="audio/pcm",
                                 headers={"X-Sample-Rate": str(SAMPLE_RATE), "X-Audio-Format": "pcm_s16le",
                                          "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return web
