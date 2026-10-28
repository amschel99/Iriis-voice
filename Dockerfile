# Iriis cloud core.  docker build -t iriis . && docker run -p 8000:8000 --env-file .env iriis
#
# Stage 1 converts the STT models to CTranslate2 and fetches the TTS voices, so the running
# container never downloads a model. PyTorch only exists in this stage. Two STT models ship,
# so you can switch on Railway (one variable, no rebuild):
#   IRIIS__STT__MODEL=models/stt/faster-whisper-small.en          (default) stock Whisper, MIT
#   IRIIS__STT__MODEL=models/stt/afrispeech-whisper-medium-ct2    AfriSpeech fine-tune, Apache-2.0
# Choose with scripts/benchmark_stt.py on your own recordings.
# Others: --build-arg STT_MODELS="small.en intron-medium professor-small" (professor-* are non-commercial)

FROM python:3.12-slim AS models
ARG STT_MODELS="small.en intron-medium"
WORKDIR /build
COPY scripts/requirements-convert.txt scripts/
RUN pip install --no-cache-dir -r scripts/requirements-convert.txt
COPY scripts/convert_models.py scripts/download_models.py scripts/
RUN python scripts/convert_models.py --models ${STT_MODELS} \
 && python scripts/download_models.py --only tts --skip-ollama \
 && rm -rf /root/.cache/huggingface

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
    IRIIS__LLM__BACKEND=openai
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libsndfile1 graphviz fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY --from=models /build/models ./models
COPY iriis ./iriis
COPY servers ./servers
COPY config ./config
COPY skills ./skills
# The rest of the project, so Iriis can read how she works (servers/self_knowledge.py).
COPY README.md pyproject.toml requirements.txt requirements-server.txt ./
COPY scripts ./scripts
COPY deploy ./deploy
COPY bin ./bin
RUN mkdir -p data logs
ENV IRIIS__STT__BACKEND=faster_whisper IRIIS__STT__MODEL=models/stt/faster-whisper-small.en
# Storage: set DATABASE_URL (Railway Postgres) to persist memories, notes, reminders and the audit log.
# Without it the container falls back to SQLite in data/, which is lost on redeploy.
EXPOSE 8000
CMD ["python", "-m", "iriis.server"]
