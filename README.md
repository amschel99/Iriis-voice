# Iriis

A personal voice assistant with an accent-tuned ear (AfriSpeech Whisper), a cloud brain (DeepSeek V4 Flash, or Groq), and
hands on your own computer (MCP tools). Other apps can talk to it over REST/SSE, a voice WebSocket, or MCP.

```
 Other tools / agents ──REST·SSE · WebSocket voice · MCP · llms.txt──┐
                                                                     ▼
            ┌──────────── Iriis Core (Docker → Railway) ────────────────┐
            │ FastAPI: /v1/chat (SSE)  /v1/voice (WS)  /mcp  /llms.txt  │
            │ Agent loop · permission tiers · audit log · memory        │
            │ LLM: DeepSeek V4 Flash (or Groq), OpenAI-compatible API   │
            │ STT: AfriSpeech Whisper (CTranslate2) · TTS: Kokoro       │
            │ Cloud tools: time & calc, notes/reminders/timers, memory  │
            └──────────────────────────▲─────────────────────────────────┘
                                       │ outbound WebSocket (no open ports)
            ┌──────────────────────────┴──── Desktop Agent (your PC) ────┐
            │ Wake word "Hey Jarvis" + VAD + mic/speaker + barge-in     │
            │ Local tools: files, shell, apps & media, clipboard, system│
            │ Re-checks every permission locally; local audit log       │
            └────────────────────────────────────────────────────────────┘
   Offline fallback: `python -m iriis --offline` runs everything on the PC (Ollama).
```

## Contents
- [Quick start (cloud + desktop)](#1-quick-start)
- [The `iriis` command](#the-iriis-command-git-bash)
- [Deploy to Railway](#2-deploy-to-railway)
- [Run the desktop agent](#3-desktop-agent)
- [Use Iriis from other tools (API, MCP, llms.txt)](#4-use-iriis-from-other-tools)
- [Permissions and safety](#5-permissions-and-safety)
- [Speech recognition and your accent](#6-speech-recognition-and-your-accent)
- [Fully local / offline mode](#7-fully-local--offline-mode)
- [Configuration](#8-configuration)
- [Adding MCP servers](#9-adding-mcp-servers)
- [Run on startup](#10-run-on-startup)
- [Troubleshooting](#11-troubleshooting)
- [Project layout and tests](#12-project-layout-and-tests)

## 1. Quick start

You need Python 3.11+ and a DeepSeek API key (https://platform.deepseek.com/api_keys).

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python scripts/download_models.py --skip-ollama     # wake word + TTS voices (~150 MB)
```

Make the speech-recognition models. This is a one-time conversion that needs PyTorch, so it runs in its own venv:
```bash
python -m venv .venv-convert
.venv-convert\Scripts\pip install -r scripts/requirements-convert.txt
.venv-convert\Scripts\python scripts/convert_models.py --models small.en intron-medium
```

Then deploy her brain (§2) and connect this PC with the `iriis` command (next section).

**Trying everything on one machine:** run `iriis server` in one terminal, and in a second terminal run
`iriis setup` (URL `http://127.0.0.1:8000`) followed by `iriis start`.
**Text-only debugging** (no server, everything in one process): `python -m iriis --text`

## The `iriis` command (Git Bash)

Put the `bin/` folder on your PATH, once:
```bash
echo 'export PATH="$PATH:/c/Users/ADMIN/rift/iriis-voice/bin"' >> ~/.bashrc && source ~/.bashrc
```
(For cmd/PowerShell, add `C:\Users\ADMIN\rift\iriis-voice\bin` to PATH; it uses `bin\iriis.cmd`.)

| Command | What it does |
|---|---|
| `iriis setup` | Saves her Railway URL and keys to `.env` (git-ignored). Press Enter to generate fresh tokens; then copy the same values into Railway's Variables |
| `iriis start` | Starts her in the background (desktop tools + wake word + voice) and reports whether she reached her brain. Flags: `--no-voice`, `--push-to-talk`, `--quiet` |
| `iriis status` | Shows whether she's running, whether her brain on Railway is online, and whether this PC is linked to it. Ends with her own one-liner, e.g. "I'm connected to my brain and ready." or "I'm not connected to my brain right now: …". `--say` speaks it too. Exit code 0 = all good |
| `iriis ask "…"` | Asks her something from the terminal and streams the reply. `--speak` plays her voice; `-v` shows tool calls and timings |
| `iriis say "…"` | She says it out loud (her cloud voice, or her on-device voice when offline) |
| `iriis logs [-f]` | Shows the background log (`logs/desktop.out`) |
| `iriis stop` / `iriis restart` | Stops or restarts the background process |
| `iriis server` | Runs the brain locally (development) |

**When the link drops:** she says "I've lost connection to my brain. I'll keep trying to reconnect." and
reconnects on her own (backoff of up to 30 s). If you talk to her while she's disconnected, she answers with her
on-device voice: "Sorry, I'm not connected to my brain right now…". When the link comes back she says
"I'm connected to my brain again."

In the background, risky actions are confirmed through the cloud (by voice or chat), because there's no
terminal to ask in. For `--confirm local`, run `python -m iriis.desktop --confirm local --voice` in a terminal instead.

## 2. Deploy to Railway

1. Push this folder to a GitHub repo. `.gitignore` keeps `models/`, `.env` and the venvs out.
2. In Railway: **New Project → Deploy from GitHub repo**. `railway.toml` tells Railway to build the `Dockerfile`.
   - The first build takes a while (roughly 10–20 minutes). It installs CPU PyTorch in a throwaway stage and
     converts two speech-recognition models (stock Whisper small.en and AfriSpeech medium), so the running
     container never downloads anything.
   - **No GitHub?** Install the Railway CLI and run `railway up` in this folder instead.
3. **Variables:** set `DEEPSEEK_API_KEY`, `IRIIS_API_KEY` and `IRIIS_DESKTOP_TOKEN`.
   To use Groq instead (faster first word), see the comment under `llm:` in config/config.yaml.
4. **Volume:** add one mounted at `/app/data`, so memories, notes and reminders survive redeploys.
5. **Networking:** click **Generate Domain**. You'll get something like `https://iriis-production.up.railway.app`.
6. **Size:** give the service at least **2 GB RAM** (small.en about 0.35 GB, AfriSpeech medium about 1 GB, Kokoro about 0.3 GB).
   More vCPUs means faster speech recognition.
7. **Speech model:** small.en is the default. To switch, set
   `IRIIS__STT__MODEL=models/stt/afrispeech-whisper-medium-ct2`. See §6 for how to choose.

Check that it's up: `curl https://<domain>/health` should return `{"ok":true}`. Then:
```bash
curl https://<domain>/v1/status -H "Authorization: Bearer $IRIIS_API_KEY"
```

Any config key can be overridden with a variable named `IRIIS__<SECTION>__<KEY>`, for example
`IRIIS__LLM__MODEL=openai/gpt-oss-20b` or `IRIIS__TTS__VOICE=af_heart`.

## 3. Desktop agent

```bash
set IRIIS_DESKTOP_TOKEN=...     # PowerShell: $env:IRIIS_DESKTOP_TOKEN="..."
set IRIIS_API_KEY=...           # needed for --voice
python -m iriis.desktop --server wss://<domain> --voice
```

- **Tools.** The agent starts the local MCP servers listed in `desktop.servers`: system info, files,
  shell, apps and media, clipboard. It connects out to the cloud and reconnects on its own if the
  connection drops.
- **Voice.** `--voice` adds the wake word, voice detection and barge-in on your PC. The audio for each
  thing you say goes to the cloud for speech recognition, and the reply's audio streams back to your speakers.
- **Push-to-talk.** Use `--push-to-talk` and hold **F9**. Change the key with `wakeword.push_to_talk_key`.
- **Confirmations.** `--confirm local` makes the desktop also ask you in its terminal before any risky
  action, whatever the cloud says. See §5.
- **Files.** Only folders listed in `files.allowed_dirs` are visible (by default Documents, Desktop
  and Downloads).

## 4. Use Iriis from other tools

There is a machine-readable guide at **`https://<domain>/llms.txt`** and an OpenAPI UI at `/docs`.
Every call needs `Authorization: Bearer <IRIIS_API_KEY>`. WebSockets can pass `?token=` instead.

**Text with streaming (SSE):**
```bash
curl -N https://<domain>/v1/chat -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"message": "How much disk space do I have?", "stream": true, "speak": false}'
```
The stream sends these events in order:
1. `session`: carries the session id. Pass it back as `session_id` to keep the conversation going.
2. `sentence`: one per sentence, as soon as it's ready.
3. `audio`: base64 WAV, only when `speak: true`.
4. `tool` / `screen`: tool activity, and links or code meant to be shown rather than read aloud.
5. `confirm`: the request needs your OK. Answer with `POST /v1/confirm` and `{"session_id", "id", "approved": true}`.
6. `done`: the end of the turn.

**Voice (WebSocket `/v1/voice`):**
- **You send:** raw PCM16 audio, 16 kHz mono, for one utterance, then `{"type":"end_utterance"}`.
- **You get back:** `transcript`, `sentence`, and `audio` headers each followed by a binary PCM16 frame at 24 kHz.
- **To interrupt** Iriis mid-answer (barge-in), send `{"type":"interrupt"}`.
- [iriis/desktop/voice_client.py](iriis/desktop/voice_client.py) is a complete client to copy from.

**Other endpoints:**
- `POST /v1/tts {"text"}` returns WAV.
- `POST /v1/stt` takes a multipart `file` and returns the text, using the accent-tuned model.
- `DELETE /v1/sessions/{id}` resets a conversation.

**MCP:** Iriis is itself an MCP server at `https://<domain>/mcp` (streamable HTTP). It offers three tools:
- `ask_iriis` sends a request and returns the reply.
- `speak` returns WAV audio in Iriis' voice.
- `iriis_status` reports health.

Example client config:
```json
{"mcpServers": {"iriis": {"type": "http", "url": "https://<domain>/mcp",
  "headers": {"Authorization": "Bearer <IRIIS_API_KEY>"}}}}
```
Over MCP, confirm-tier actions are declined, because MCP has no way for you to say yes. Do those by voice or in chat.

## 5. Permissions and safety

Every tool has a tier:

| Tier | Behaviour | Examples |
|---|---|---|
| `auto` | Runs straight away | system info, time, search/read files, read notes, `git status` |
| `confirm` | Iriis says exactly what it will do and waits for your "yes" (anything else cancels) | write/move/delete files, non-allowlisted shell commands, closing apps, opening URLs, setting the clipboard |
| `blocked` | Never runs | `rm -rf /`, formatting disks, `curl … \| sh`, reading `.env`/SSH keys/credential stores |

- **Where tiers come from.** Server defaults live in [config/mcp_servers.yaml](config/mcp_servers.yaml) and
  global overrides in `permissions.overrides`. Any tool not covered there defaults to `confirm`.
- **Shell commands.** The tier depends on the command itself, using an allowlist and a blocklist
  ([iriis/safety/shell_policy.py](iriis/safety/shell_policy.py)).
- **The desktop has the final say.** It re-checks every call with its own config. `blocked` is refused
  on the PC whatever the cloud says. For confirm-tier tools:
  - `confirm_mode: cloud` (the default): the PC runs the call only if the cloud says you confirmed it.
  - `confirm_mode: local`: the PC also asks in its own terminal. This is the safest mode. It protects
    you even if the cloud service or its API key were compromised.
- **Prompt injection.** Tool output (files, web pages, messages) is wrapped and marked as untrusted. The
  model is told never to follow instructions found inside it. A tool result can't raise permissions or
  approve a confirmation. Only your live reply counts, and it must be clearly affirmative: "yes",
  "go ahead" and similar. "Yes… no wait" counts as no.
- **Secrets.** Tool results and logs are scrubbed of API keys, tokens and `password=` lines. The files
  server refuses `.env`, key files and credential stores.
- **Audit log.** `logs/audit.jsonl` records every tool call, on both the cloud and the desktop: time,
  tool, redacted arguments, tier, whether it was confirmed, and a summary of the result.
- **Dry run.** `--dry-run` (or `permissions.dry_run: true`) logs tool calls without running them.
- **What goes to the cloud.** Your words (transcribed in your own Railway container) and tool results go
  to Groq for the LLM step. Audio never goes to Groq. If you don't want a request to leave your PC, use
  offline mode (§7).

## 6. Speech recognition and your accent

| Model | Tuned on | Licence | Notes |
|---|---|---|---|
| `small.en`: Systran/faster-whisper-small.en | General English | MIT | **Current default**, fast |
| `intron-medium`: intronhealth/afrispeech-whisper-medium-all | AfriSpeech-200 (120+ African accents) | Apache-2.0 | Accent-tuned, slower; strong clinical-domain bias |
| `professor-small`: Professor/whisper-small-afrispeech | AfriSpeech-200 | CC-BY-NC-SA-4.0 | Accent-tuned, faster; personal/non-commercial use only |
| Parakeet TDT 0.6B v2 (sherpa-onnx) | General English | CC-BY-4.0 | Very fast on CPU; not accent-tuned |

**What has been measured so far (synthetic test).** The test used six of the benchmark commands spoken
by Kokoro's American and British voices, so these are *not* Kenyan accents:
- **small.en:** 10% WER.
- **AfriSpeech medium:** 56% WER. It turns commands into clinical phrases, for example "Set a timer for
  10 minutes" became "sedation of 10 minutes". It was fine-tuned on AfriSpeech, which is largely medical.

On African-accented speech the ranking may flip, and that is the whole point of the fine-tune. Two lessons from debugging:
- **The vocabulary prompt.** Feeding `stt.vocabulary` to the AfriSpeech model as a prompt wrecked its
  output ("7."). That's why `stt.use_vocabulary_prompt` is off by default.
- **The converter fix.** `convert_models.py` now restores Whisper's suppressed-token list for fine-tunes that don't ship one.

**Measure it on your own voice. Don't assume which model is best.**
```bash
python scripts/record_benchmark.py          # walks you through 30 typical commands, saves WAVs
python scripts/benchmark_stt.py --audio-dir benchmarks/my_voice --write-config
```
`benchmark_stt.py` prints word error rate (WER), real-time factor and RAM for every model in `models/stt/`.
It picks the most accurate model within the latency budget and, with `--write-config`, sets it as the default.

- **Kenyan subset of AfriSpeech.** You can also test on part of AfriSpeech-200 with
  `--afrispeech 40 --accents swahili kikuyu luo luhya`. The dataset is gated: accept its terms on
  Hugging Face, run `huggingface-cli login`, and `pip install "datasets<4" librosa`.
- **Proper nouns.** Add names of people, places and projects to `stt.vocabulary` in
  [config/config.yaml](config/config.yaml). Parakeet uses them as hotwords. For Whisper they're only used
  when `use_vocabulary_prompt: true`; benchmark with and without it.

**Latency:**
```bash
python scripts/benchmark_latency.py --server wss://<domain> --audio-dir benchmarks/my_voice
```
It reports end of speech → transcript, first LLM token, first sentence, first audio, and total.

## 7. Fully local / offline mode

Everything runs on the PC with no network: wake word, VAD, Whisper, Ollama, Kokoro/Piper and the MCP tools.
```bash
# install Ollama from https://ollama.com, then:
ollama pull qwen3:4b-instruct-2507-q4_K_M
python -m iriis --offline          # voice
python -m iriis --offline --text   # typing
python scripts/verify_offline.py --llm   # proves everything loads with networking blocked
```
- **It's slow on a busy CPU.** On this development laptop (i7-1185G7 with only about 3 GB of RAM free),
  the local 4B model took 60–80 s for its first token, against a 1.5 s target. That's why the cloud
  core is the default. On a free machine with 8 GB of spare RAM, `qwen3:8b` is the better local model.
- **Memory.** The local stack uses about 0.4 GB for the Iriis process plus about 3 GB for Ollama.

## 8. Configuration

The settings live in [config/config.yaml](config/config.yaml), with comments on every key:
- audio devices
- wake word and push-to-talk
- VAD timings
- STT model and vocabulary
- LLM provider and models
- TTS voice and speed
- barge-in
- permissions and allowlisted folders
- server and desktop settings

The settings are validated by pydantic ([iriis/config.py](iriis/config.py)).

- **Secrets only come from environment variables:** `DEEPSEEK_API_KEY` (or `GROQ_API_KEY`), `IRIIS_API_KEY` and
  `IRIIS_DESKTOP_TOKEN`. They never go in YAML, prompts or logs.
- **Switching LLM provider** means changing `llm.base_url`, `llm.api_key_env` and `llm.model`. Any
  OpenAI-compatible API works: OpenRouter, OpenAI, Together, or a local llama.cpp server.
- **Audio devices:** run `python -m iriis --list-devices`, then set `audio.input_device` or
  `audio.output_device` to an index or part of the device name.

## 9. Adding MCP servers

Add an entry under `mcpServers` in [config/mcp_servers.yaml](config/mcp_servers.yaml):
```yaml
  my_server:
    command: npx                      # or python, uvx, a path...
    args: ["-y", "some-mcp-server"]
    env: {SOME_TOKEN_FILE: "C:/path"} # credentials stay in the server's env
    tier: confirm                     # default for its tools
    tools: {list_*: auto, delete_*: blocked}
    description: What it does (used to route requests).
    keywords: [words, that, route, here]
    needs_internet: true
```
- **Where it runs.** Put its name in `desktop.servers` to run it on your PC, or in `server.cloud_servers`
  to run it in the container.
- **Reloading.** Say "reload tools" to pick up the change.
- **Naming.** Tool names reach the LLM as `server__tool`.
- **Many tools.** Above 20 tools, only the servers relevant to each request are offered (`tool_routing`).
- **Ready-made examples.** The file includes disabled examples for the reference filesystem and git
  servers, Playwright, and unofficial WhatsApp, X, Instagram and LinkedIn bridges.
- **About those unofficial bridges.** They need internet and can break without warning. Using them can
  get your account banned. Keep them read-mostly; their send and post tools stay at `confirm`.

## 10. Run on startup

These start the **desktop agent** at login. The core runs on Railway.

**Windows (Task Scheduler):**
```powershell
$py = "$PWD\.venv\Scripts\pythonw.exe"
$act = New-ScheduledTaskAction -Execute $py -Argument "-m iriis.desktop --server wss://<domain> --voice" -WorkingDirectory $PWD
Register-ScheduledTask -TaskName "Iriis" -Action $act -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Limited
```
Set `IRIIS_DESKTOP_TOKEN` and `IRIIS_API_KEY` as *user* environment variables (System Properties →
Environment Variables) so the task can read them.

**Linux (systemd user service)**, saved as `~/.config/systemd/user/iriis.service`:
```ini
[Unit]
Description=Iriis desktop agent
After=network-online.target sound.target
[Service]
WorkingDirectory=%h/iriis
EnvironmentFile=%h/iriis/.env
ExecStart=%h/iriis/.venv/bin/python -m iriis.desktop --server wss://<domain> --voice
Restart=on-failure
[Install]
WantedBy=default.target
```
Enable it with `systemctl --user enable --now iriis`.

**macOS (launchd):** create `~/Library/LaunchAgents/com.iriis.desktop.plist` with `ProgramArguments`
`[.venv/bin/python, -m, iriis.desktop, --server, wss://<domain>, --voice]`, `RunAtLoad` true, and
`EnvironmentVariables` holding the two tokens. Load it with `launchctl load …`. macOS will also ask
for Microphone and Accessibility permission (the latter is for push-to-talk and media keys).

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| **Mic not detected / silence** | `python -m iriis --list-devices`, then set `audio.input_device`. On Windows, check Settings → Privacy → Microphone. On Linux, install `libportaudio2`. |
| **Wake word never fires** | Lower `wakeword.threshold` to 0.35–0.4. Speak at a normal volume about 1 m away. Or use `--push-to-talk`. |
| **Wake word fires by itself** | Raise `wakeword.threshold` to 0.6–0.7. |
| **It cuts me off mid-sentence** | Raise `vad.silence_ms` (700 → 1000). |
| **Iriis hears itself / interrupts itself** | Use headphones (most reliable). Or raise `barge_in.echo_gate_ratio` (1.5 → 2.5). Or set `barge_in.enabled: false`. The echo gate is a simple energy comparison, not real echo cancellation, so loud speakers next to the mic will still leak. |
| **Slow speech recognition** | On Railway, add vCPUs or use small.en. Locally, try `stt.backend: parakeet`. Use `scripts/benchmark_stt.py` to compare. |
| **First reply is slow** | Groq: check `/v1/status`. Fewer tools per turn helps (`tool_routing.max_tools`). Offline: the model may be loading; `keep_alive` keeps it in RAM. |
| **`401` from the API** | Send `Authorization: Bearer <IRIIS_API_KEY>`. The desktop link uses `IRIIS_DESKTOP_TOKEN`, a different secret. |
| **Desktop says "Disconnected … 4401"** | The token doesn't match the server's `IRIIS_DESKTOP_TOKEN`. |
| **Desktop tools missing** | `GET /v1/status` should show `desktop_connected: true`. Check the agent's terminal and `logs/mcp/<server>.log`. |
| **Hugging Face tries to download at runtime** | Run `scripts/verify_offline.py`. Models must be in `models/`. The Docker image sets `HF_HUB_OFFLINE=1`. |
| **`ZoneInfoNotFoundError` on Windows** | `pip install tzdata` (it's in requirements). |

## 12. Project layout and tests

```
iriis/
  __main__.py            all-in-one local app (text / voice / --offline)
  app.py, voice.py       local orchestration and the local voice loop
  config.py              pydantic config + IRIIS__ env overrides
  audio/                 capture, playback, wake word, push-to-talk, Silero VAD endpointing
  stt/                   faster-whisper (AfriSpeech) and Parakeet engines
  tts/                   Kokoro and Piper engines, text normalisation (KSh 1,500 -> "fifteen hundred shillings")
  llm/                   Groq/OpenAI-compatible + Ollama clients, prompts, sentence streaming
  agent/                 agent loop, permissions, tool routing, history, long-term memory, reminders
  mcp_client/            MCP client manager (mcp 2.x) and schema conversion
  safety/                shell allow/block policy
  server/                cloud core: FastAPI app, sessions, speech, desktop bridge, llms.txt
  desktop/               desktop agent + cloud voice client
servers/                 built-in MCP servers (MCPServer / mcp 2.x)
scripts/                 download/convert models, STT + latency benchmarks, recorder, offline check
config/                  config.yaml, mcp_servers.yaml
Dockerfile, railway.toml, requirements*.txt
```
Run the tests with `pytest`. They cover:
- permissions, the shell policy and confirmations
- text normalisation and sentence streaming
- schema conversion and tool routing
- the server API: auth, SSE, desktop round trip, and confirm/decline
- the Groq client: streamed tool calls and rate-limit fallback
- the built-in servers' logic and the config files
