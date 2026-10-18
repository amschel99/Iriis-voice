# Iriis' own Orpheus voice: one image for Hugging Face Inference Endpoints, RunPod, or any NVIDIA GPU box.
# Built and published by .github/workflows/orpheus-image.yml -> ghcr.io/<owner>/iriis-orpheus:latest
#
# Needs at runtime: HF_TOKEN (Hugging Face read token with access to canopylabs/orpheus-3b-0.1-ft)
#                   ORPHEUS_TOKEN (the secret Iriis sends)          GPU: 24 GB (A10G / L4 / A5000 / 3090 / 4090)
FROM vllm/vllm-openai:v0.9.2

RUN pip install --no-cache-dir snac==1.2.1 soundfile==0.13.1 hf_transfer==0.1.9 "fastapi[standard]==0.115.14" \
 && apt-get update && apt-get install -y --no-install-recommends libsndfile1 && rm -rf /var/lib/apt/lists/*

ENV HF_HOME=/data/hf HF_HUB_ENABLE_HF_TRANSFER=1 VLLM_LOGGING_LEVEL=WARNING PORT=80 PYTHONUNBUFFERED=1
WORKDIR /app
COPY __init__.py core.py server.py /app/orpheus_server/

EXPOSE 80
ENTRYPOINT []
CMD ["python3", "-m", "orpheus_server.server"]
