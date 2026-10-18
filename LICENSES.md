# Dependencies and licences

Iriis' own code: MIT.

## Flags: read these first

- **The Piper TTS engine (`piper-tts` 1.8) and the espeak-ng phonemizer are GPL-3.0.** espeak-ng is used by
  both Kokoro (through `espeakng-loader`) and Piper. Using them for yourself creates no obligations. If you
  *distribute* a product containing them, you must follow the GPL. There is no permissively licensed
  English phonemizer of comparable quality.
- **`Professor/whisper-small-afrispeech` and `whisper-medium-afrispeech` are CC-BY-NC-SA-4.0** (non-commercial).
  The models shipped in the image, `faster-whisper-small.en` (MIT) and `intronhealth/afrispeech-whisper-medium-all` (Apache-2.0), are both fine.
- **The AfriSpeech-200 dataset is CC-BY-NC-SA-4.0 and gated.** It's only used by the optional benchmark.
- **Groq is a paid hosted API**, under its own terms of service. The gpt-oss model weights are Apache-2.0. Offline mode avoids Groq entirely.
- **Third-party messaging bridges** (WhatsApp, X, Instagram, LinkedIn) are unofficial and may break the
  platforms' terms of service. They're disabled by default.

## Python packages (pinned in requirements*.txt)

| Package | Version | Licence | Used for |
|---|---|---|---|
| mcp | 2.3.0 | MIT | MCP client + servers + Iriis' own /mcp |
| fastapi | 0.143.5 | MIT | Cloud API |
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI server |
| websockets | 17.2 | BSD-3-Clause | Desktop and voice links |
| python-multipart | 0.0.32 | Apache-2.0 | /v1/stt uploads |
| httpx | 0.28.1 | BSD-3-Clause | LLM HTTP client |
| pydantic | 2.14.1 | MIT | Config validation |
| PyYAML | 6.0.3 | MIT | Config files |
| rich | 15.0.0 | MIT | Console UI |
| psutil | 7.2.2 | BSD-3-Clause | System info server, memory checks |
| numpy | 2.5.4 | BSD-3-Clause | Audio arrays |
| soundfile | 0.14.0 | BSD-3-Clause (libsndfile: LGPL-2.1) | WAV I/O |
| tzdata | 2026.5 | Apache-2.0 | Timezones on Windows/containers |
| pint | 0.26.1 | BSD-3-Clause | Unit conversion |
| num2words | 0.5.14 | LGPL-2.1 | Numbers to words for TTS |
| onnxruntime | 1.31.0 | MIT | VAD, wake word, Kokoro, Piper |
| faster-whisper | 1.2.1 | MIT | Whisper inference |
| ctranslate2 | 4.8.3 | MIT | Whisper runtime + conversion |
| kokoro-onnx | 0.6.1 | MIT | Kokoro TTS runtime |
| espeakng-loader (via kokoro-onnx) | — | GPL-3.0 (espeak-ng) | Phonemes |
| piper-tts | 1.8.0 | **GPL-3.0-or-later** | Fallback TTS |
| psycopg[binary] | 3.3.6 | LGPL-3.0 (bundled libpq: PostgreSQL licence) | Postgres storage (Railway) |
| sounddevice | 0.5.6 | MIT (PortAudio: MIT-style) | Mic/speaker (desktop only) |
| openwakeword | 0.6.0 | Apache-2.0 (code) | Wake word (desktop only) |
| pynput | 1.8.2 | LGPL-3.0 | Push-to-talk key (desktop only) |
| pyperclip | 1.11.0 | BSD-3-Clause | Clipboard server (desktop only) |
| sherpa-onnx | 1.13.8 | Apache-2.0 | Parakeet STT (optional) |
| jiwer | 4.0.0 | Apache-2.0 | WER in the benchmark |
| ollama | 0.6.3 | MIT | (offline tooling) |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | MIT / Apache-2.0 | Tests |
| torch (convert venv only) | 2.14.1 | BSD-3-Clause | Model conversion |
| transformers (convert venv only) | 4.57.3 | Apache-2.0 | Model conversion |
| huggingface_hub (convert venv only) | 0.36.0 | Apache-2.0 | Model download |

## Models

| Model | Licence | Where |
|---|---|---|
| intronhealth/afrispeech-whisper-medium-all | Apache-2.0 | Accent-tuned STT, shipped in the image (converted to CTranslate2) |
| Professor/whisper-small-afrispeech | **CC-BY-NC-SA-4.0** | Optional faster STT |
| Systran/faster-whisper-small.en (OpenAI Whisper) | MIT | **Current default STT** (until your accent benchmark decides) |
| NVIDIA Parakeet TDT 0.6B v2 (sherpa-onnx int8 export) | CC-BY-4.0 | Optional STT |
| Silero VAD v6 (bundled with faster-whisper) | MIT | Voice activity detection |
| openWakeWord `hey_jarvis` + feature models | Apache-2.0 code; the pretrained models are **CC-BY-NC-SA-4.0** according to the openWakeWord README | Wake word. Train your own wake word for commercial use |
| Orpheus TTS (canopylabs/orpheus-v1-english, served by Groq) | Apache-2.0 weights; Groq ToS + model terms for the service (paid, $22 per 1M characters) | Expressive TTS (default when GROQ_API_KEY is set) |
| canopylabs/orpheus-3b-0.1-ft (self-hosted, deploy/orpheus_modal.py) | Card says Apache-2.0; it is fine-tuned from Llama-3.2-3B-Instruct, so the **Llama 3.2 Community License** may also apply. Gated on Hugging Face | Expressive TTS on your own GPU |
| SNAC 24 kHz (hubertsiuzdak/snac_24khz) | MIT | Orpheus audio decoder |
| vLLM 0.9.2 / Modal SDK 1.6.2 | Apache-2.0 / Apache-2.0 | GPU inference / deployment |
| Kokoro-82M v1.0 (ONNX) | Apache-2.0 | TTS fallback and on-device voice |
| Piper voice en_GB-alba-medium | See the voice's MODEL_CARD on Hugging Face (rhasspy/piper-voices) | Fallback TTS |
| openai/gpt-oss-120b, gpt-oss-20b (served by Groq) | Apache-2.0 weights; Groq ToS for the service | Cloud LLM |
| qwen3:4b-instruct-2507 / qwen3:8b (Ollama) | Apache-2.0 | Offline LLM |

## Runtimes

| Software | Licence |
|---|---|
| Ollama | MIT |
| Python | PSF |
| PortAudio | MIT-style |
