"""Self-hosted Orpheus TTS for Iriis, on a Modal serverless GPU (scales to zero when idle).

    pip install modal==1.6.2 && modal token new
    modal secret create iriis-orpheus HF_TOKEN=hf_xxx ORPHEUS_TOKEN=<random secret>
    modal deploy deploy/orpheus_modal.py

Speaks the same API as Groq's /audio/speech, so Iriis just points at a different URL:
    POST {url}/v1/audio/speech   {"model": "...", "input": "text", "voice": "tara"}  -> audio/wav (24 kHz)
    GET  {url}/health
Auth: Authorization: Bearer <ORPHEUS_TOKEN>.

Model: canopylabs/orpheus-3b-0.1-ft (gated: accept its terms on Hugging Face first).
Voices: tara, leah, jess, mia, zoe (female), leo, dan, zac (male).
Sound tags: <laugh> <chuckle> <sigh> <cough> <sniffle> <groan> <yawn> <gasp>.
Decoding follows canopyai/Orpheus-TTS (prompt tokens, 7-token SNAC frames, 24 kHz).
"""
from __future__ import annotations

import modal

APP_NAME = "iriis-orpheus"
MODEL_ID = "canopylabs/orpheus-3b-0.1-ft"
SNAC_ID = "hubertsiuzdak/snac_24khz"
VOICES = ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"]
WEIGHTS_DIR = "/weights"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "vllm==0.9.2",
        "snac==1.2.1",
        "fastapi[standard]==0.115.14",
        "soundfile==0.13.1",
        "huggingface_hub[hf_transfer]==0.33.4",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "VLLM_LOGGING_LEVEL": "WARNING"})
)
weights = modal.Volume.from_name("iriis-orpheus-weights", create_if_missing=True)
app = modal.App(APP_NAME)

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


@app.cls(
    gpu="L4",
    image=image,
    volumes={WEIGHTS_DIR: weights},
    secrets=[modal.Secret.from_name("iriis-orpheus")],
    scaledown_window=300,          # stay warm 5 min after the last sentence, then scale to zero
    startup_timeout=900,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=8)
class Orpheus:
    @modal.enter()
    def load(self) -> None:
        import os
        import threading

        import torch
        from huggingface_hub import snapshot_download
        from snac import SNAC
        from transformers import AutoTokenizer
        from vllm import LLM

        token = os.environ["HF_TOKEN"]
        local = snapshot_download(MODEL_ID, token=token, cache_dir=WEIGHTS_DIR)
        snapshot_download(SNAC_ID, cache_dir=WEIGHTS_DIR)
        weights.commit()   # keep the weights for the next cold start
        self.tokenizer = AutoTokenizer.from_pretrained(local)
        self.llm = LLM(model=local, dtype="bfloat16", max_model_len=4096, gpu_memory_utilization=0.80,
                       enforce_eager=False)
        self.snac = SNAC.from_pretrained(SNAC_ID, cache_dir=WEIGHTS_DIR).eval().to("cuda")
        self.lock = threading.Lock()
        self.torch = torch

    def _prompt_ids(self, text: str, voice: str) -> list[int]:
        ids = self.tokenizer(f"{voice}: {text}").input_ids          # includes <|begin_of_text|>
        return [START_OF_HUMAN, *ids, END_OF_TEXT, END_OF_HUMAN, START_OF_AI, START_OF_SPEECH]

    def _decode(self, codes: list[int]):
        torch = self.torch
        frames = len(codes) // 7
        c0, c1, c2 = [], [], []
        for j in range(frames):
            f = codes[7 * j: 7 * j + 7]
            c0.append(f[0])
            c1 += [f[1], f[4]]
            c2 += [f[2], f[3], f[5], f[6]]
        layers = [torch.tensor(c, dtype=torch.int32, device="cuda").unsqueeze(0) for c in (c0, c1, c2)]
        with torch.inference_mode():
            audio = self.snac.decode(layers)
        return audio.squeeze().float().cpu().numpy()

    def synthesize(self, text: str, voice: str, temperature: float, top_p: float, repetition_penalty: float):
        from vllm import SamplingParams
        params = SamplingParams(temperature=temperature, top_p=top_p, repetition_penalty=repetition_penalty,
                                max_tokens=3600, stop_token_ids=[END_OF_SPEECH])
        with self.lock:
            out = self.llm.generate([{"prompt_token_ids": self._prompt_ids(text, voice)}], params, use_tqdm=False)
        codes = codes_from_token_ids(list(out[0].outputs[0].token_ids))
        if len(codes) < 7:
            raise ValueError("the model produced no audio")
        return self._decode(codes)

    @modal.asgi_app()
    def api(self):
        import hmac
        import io
        import os

        import soundfile as sf
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.responses import Response
        from pydantic import BaseModel

        web = FastAPI(title="Iriis Orpheus TTS")
        secret = os.environ.get("ORPHEUS_TOKEN", "")

        def check(authorization: str | None) -> None:
            token = (authorization or "").removeprefix("Bearer ").strip()
            if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
                raise HTTPException(401, "unauthorised")

        class Speech(BaseModel):
            input: str
            voice: str = "tara"
            model: str = MODEL_ID
            response_format: str = "wav"
            temperature: float = 0.6
            top_p: float = 0.8
            repetition_penalty: float = 1.3

        @web.get("/health")
        def health():
            return {"ok": True, "model": MODEL_ID, "voices": VOICES}

        @web.post("/v1/audio/speech")
        def speech(req: Speech, authorization: str | None = Header(default=None)):
            check(authorization)
            if req.voice not in VOICES:
                raise HTTPException(400, f"unknown voice {req.voice!r}; choose from {VOICES}")
            if not req.input.strip() or len(req.input) > 1200:
                raise HTTPException(400, "input must be 1-1200 characters")
            audio = self.synthesize(req.input.strip(), req.voice, req.temperature, req.top_p, req.repetition_penalty)
            buf = io.BytesIO()
            sf.write(buf, audio, 24000, format="WAV", subtype="PCM_16")
            return Response(buf.getvalue(), media_type="audio/wav")

        return web


    @modal.method()
    def synth_wav(self, text: str, voice: str = "tara") -> bytes:
        """For `modal run` smoke tests: returns WAV bytes."""
        import io

        import soundfile as sf
        buf = io.BytesIO()
        sf.write(buf, self.synthesize(text, voice, 0.6, 0.8, 1.3), 24000, format="WAV", subtype="PCM_16")
        return buf.getvalue()


@app.local_entrypoint()
def smoke(text: str = "Hey, it's Iriis. <laugh> I'm running on my own GPU now.", voice: str = "tara"):
    """modal run deploy/orpheus_modal.py --text "..."  -> writes orpheus_test.wav locally."""
    wav = Orpheus().synth_wav.remote(text, voice)
    with open("orpheus_test.wav", "wb") as f:
        f.write(wav)
    print(f"wrote orpheus_test.wav ({len(wav) / 48000:.1f} s)")
