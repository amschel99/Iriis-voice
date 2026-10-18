"""Iriis' own Orpheus voice server: canopylabs/orpheus-3b-0.1-ft on vLLM, decoded with SNAC (24 kHz).

Host-agnostic: the same code runs in the Docker image (Hugging Face Inference Endpoints, RunPod, any GPU box)
and on Modal. API (Groq-compatible, so Iriis just changes the URL):
    GET  /health                 -> 200 once the model is loaded (503 while loading)
    POST /v1/audio/speech        {"input": "...", "voice": "tara"} -> audio/wav, 24 kHz mono
Auth: X-Orpheus-Token: <ORPHEUS_TOKEN> (some hosts, e.g. Hugging Face Endpoints, strip Authorization),
      or Authorization: Bearer <ORPHEUS_TOKEN>.
Env: ORPHEUS_QUANTIZATION=fp8 (default) or "" for full bf16.

Decoding follows canopyai/Orpheus-TTS: prompt = [START_OF_HUMAN] + tokens("voice: text") + [END markers],
generated <custom_token_N> ids -> 7-token frames -> 3 SNAC codebooks -> audio.
"""
from __future__ import annotations

import hmac
import io
import os
import threading

MODEL_ID = os.environ.get("ORPHEUS_MODEL", "canopylabs/orpheus-3b-0.1-ft")
SNAC_ID = "hubertsiuzdak/snac_24khz"
VOICES = ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"]
SAMPLE_RATE = 24000

# Special tokens (Llama-3.2 vocab + Orpheus audio tokens)
START_OF_HUMAN, END_OF_TEXT, END_OF_HUMAN, START_OF_AI, START_OF_SPEECH = 128259, 128009, 128260, 128261, 128257
END_OF_SPEECH = 128258
AUDIO_TOKEN_BASE = 128256 + 10        # <custom_token_N> has id 128256 + N; audio codes start at N = 10


def codes_from_token_ids(token_ids: list[int]) -> list[int]:
    """Generated token ids -> SNAC codes, applying Orpheus' per-position offset (index % 7) * 4096."""
    codes: list[int] = []
    for tid in token_ids:
        if tid == END_OF_SPEECH:
            break
        code = tid - AUDIO_TOKEN_BASE - (len(codes) % 7) * 4096
        if 0 <= code < 4096:
            codes.append(code)
        # anything else (start-of-speech markers etc.) is skipped without advancing the frame position
    return codes[: len(codes) // 7 * 7]


def split_codebooks(codes: list[int]) -> tuple[list[int], list[int], list[int]]:
    c0, c1, c2 = [], [], []
    for j in range(len(codes) // 7):
        f = codes[7 * j: 7 * j + 7]
        c0.append(f[0])
        c1 += [f[1], f[4]]
        c2 += [f[2], f[3], f[5], f[6]]
    return c0, c1, c2


class OrpheusModel:
    def __init__(self, cache_dir: str | None = None, gpu_memory_utilization: float = 0.80):
        self.cache_dir = cache_dir
        self.gpu_memory_utilization = gpu_memory_utilization
        self.ready = False
        self.error = ""
        self._lock = threading.Lock()

    def load(self) -> None:
        try:
            import torch
            from huggingface_hub import snapshot_download
            from snac import SNAC
            from transformers import AutoTokenizer
            from vllm import LLM

            token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            local = snapshot_download(MODEL_ID, token=token, cache_dir=self.cache_dir)
            self.tokenizer = AutoTokenizer.from_pretrained(local)
            # fp8 weights halve the memory read per token; speech generation is bandwidth-bound, so this
            # roughly doubles speed (real time needs ~84 tokens/s) with negligible quality loss.
            quant = os.environ.get("ORPHEUS_QUANTIZATION", "fp8") or None
            self.llm = LLM(model=local, dtype="bfloat16", max_model_len=4096, quantization=quant,
                           gpu_memory_utilization=self.gpu_memory_utilization)
            self.snac = SNAC.from_pretrained(SNAC_ID, cache_dir=self.cache_dir).eval().to("cuda")
            self.torch = torch
            self.ready = True
        except Exception as e:  # noqa: BLE001  (reported by /health)
            self.error = f"{e.__class__.__name__}: {e}"
            raise

    def _prompt_ids(self, text: str, voice: str) -> list[int]:
        ids = self.tokenizer(f"{voice}: {text}").input_ids          # includes <|begin_of_text|>
        return [START_OF_HUMAN, *ids, END_OF_TEXT, END_OF_HUMAN, START_OF_AI, START_OF_SPEECH]

    def synthesize(self, text: str, voice: str = "tara", temperature: float = 0.6, top_p: float = 0.8,
                   repetition_penalty: float = 1.3):
        from vllm import SamplingParams
        torch = self.torch
        params = SamplingParams(temperature=temperature, top_p=top_p, repetition_penalty=repetition_penalty,
                                max_tokens=3600, stop_token_ids=[END_OF_SPEECH])
        with self._lock:
            out = self.llm.generate([{"prompt_token_ids": self._prompt_ids(text, voice)}], params, use_tqdm=False)
        codes = codes_from_token_ids(list(out[0].outputs[0].token_ids))
        if len(codes) < 7:
            raise ValueError("the model produced no audio")
        layers = [torch.tensor(c, dtype=torch.int32, device="cuda").unsqueeze(0) for c in split_codebooks(codes)]
        with torch.inference_mode():
            audio = self.snac.decode(layers)
        return audio.squeeze().float().cpu().numpy()

    def wav(self, text: str, voice: str = "tara", **kw) -> bytes:
        import soundfile as sf
        buf = io.BytesIO()
        sf.write(buf, self.synthesize(text, voice, **kw), SAMPLE_RATE, format="WAV", subtype="PCM_16")
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
    from fastapi.responses import JSONResponse, Response

    secret = os.environ.get("ORPHEUS_TOKEN", "")
    web = FastAPI(title="Iriis Orpheus TTS")

    if load_in_background:
        @web.on_event("startup")
        def _start_loading():
            threading.Thread(target=model.load, name="orpheus-load", daemon=True).start()

    def check(authorization: str | None, x_orpheus_token: str | None) -> None:
        token = (x_orpheus_token or (authorization or "").removeprefix("Bearer ")).strip()
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            raise HTTPException(401, "unauthorised")

    @web.get("/health")
    def health():
        if model.ready:
            return {"ok": True, "model": MODEL_ID, "voices": VOICES}
        return JSONResponse({"ok": False, "loading": not model.error, "error": model.error}, status_code=503)

    @web.post("/v1/audio/speech")
    def speech(req: Speech, authorization: str | None = Header(default=None),
               x_orpheus_token: str | None = Header(default=None)):
        check(authorization, x_orpheus_token)
        if not model.ready:
            raise HTTPException(503, "the voice model is still loading")
        if req.voice not in VOICES:
            raise HTTPException(400, f"unknown voice {req.voice!r}; choose from {VOICES}")
        if not req.input.strip() or len(req.input) > 1200:
            raise HTTPException(400, "input must be 1-1200 characters")
        wav = model.wav(req.input.strip(), req.voice, temperature=req.temperature, top_p=req.top_p,
                        repetition_penalty=req.repetition_penalty)
        return Response(wav, media_type="audio/wav")

    return web
