"""Iriis' own Orpheus voice on a Modal serverless GPU (scales to zero when idle).
Same server code as the Docker image (deploy/orpheus_server/core.py), so hosts are interchangeable.

    pip install modal==1.6.2 && modal token new        (Modal requires a payment method for GPUs)
    modal secret create iriis-orpheus HF_TOKEN=hf_xxx ORPHEUS_TOKEN=<random secret>
    modal deploy deploy/orpheus_modal.py                (IRIIS_ORPHEUS_GPU=L4 to pick another GPU)
    modal run deploy/orpheus_modal.py --text "..."      (smoke test: writes orpheus_test.wav)
"""
from __future__ import annotations

import os
from pathlib import Path

import modal

GPU = os.environ.get("IRIIS_ORPHEUS_GPU", "A10G")   # 24 GB with bf16: A10G or L4
WEIGHTS_DIR = "/weights"
HERE = Path(__file__).resolve().parent

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("vllm==0.9.2", "snac==1.2.1", "fastapi[standard]==0.115.14", "soundfile==0.13.1",
                 "huggingface_hub[hf_transfer]==0.33.4")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "VLLM_LOGGING_LEVEL": "WARNING"})
    .add_local_dir(str(HERE / "orpheus_server"), remote_path="/root/orpheus_server")
)
weights = modal.Volume.from_name("iriis-orpheus-weights", create_if_missing=True)
app = modal.App("iriis-orpheus")


@app.cls(gpu=GPU, image=image, volumes={WEIGHTS_DIR: weights}, secrets=[modal.Secret.from_name("iriis-orpheus")],
         scaledown_window=300, startup_timeout=900, timeout=600, max_containers=1)
@modal.concurrent(max_inputs=8)
class Orpheus:
    @modal.enter()
    def load(self) -> None:
        from orpheus_server.core import OrpheusModel
        self.model = OrpheusModel(cache_dir=WEIGHTS_DIR)
        self.model.load()
        weights.commit()   # keep the weights for the next cold start

    @modal.asgi_app()
    def api(self):
        from orpheus_server.core import create_app
        return create_app(self.model, load_in_background=False)

    @modal.method()
    async def synth_wav(self, text: str, voice: str = "tara") -> bytes:
        return await self.model.wav(text, voice)


@app.local_entrypoint()
def smoke(text: str = "Hey, it's Iriis. <laugh> I'm running on my own GPU now.", voice: str = "tara"):
    wav = Orpheus().synth_wav.remote(text, voice)
    Path("orpheus_test.wav").write_bytes(wav)
    print(f"wrote orpheus_test.wav ({len(wav) / 48000:.1f} s)")
