# Setting up the whole system on a new machine

This guide is for anyone setting the system up on their own computer. It covers the three parts, what each needs, every setting, and how to use it day to day.

| Part | What it is | Repo |
|---|---|---|
| **Iriis** | A voice assistant: talk, Telegram, web, email, files, tasks, money, documents | `github.com/amschel99/Iriis-voice` |
| **Seryn Twin** | Seryn Labs' digital twin: cited data, models, lab protocols, papers | `github.com/Rift-FI/Seryn-twin` (private) |
| **Seryn Vision** | A bench-camera assistant: phone camera, live readings, voice guidance, video reviews | `github.com/Rift-FI/iriis-visual` (private) |

Each part runs on its own. They connect to each other through MCP links protected by keys:
- Iriis can use Seryn and the bench camera.
- Seryn Vision can use Seryn's data.

You can run any part locally, use the copies already on Railway, or mix the two.

---

## 1. Before you start

| Need | Why | How (Windows) |
|---|---|---|
| Windows 10/11, macOS or Linux | | |
| 16 GB RAM (8 GB is the minimum) | The vision model uses about 3–4 GB | |
| About 25 GB free disk | Models, Python libraries | |
| Python 3.12 | All three parts | `winget install Python.Python.3.12` |
| Git | Getting the code | `winget install Git.Git` |
| Node.js LTS | Iriis's browser control | `winget install OpenJS.NodeJS.LTS` |
| Google Chrome | Iriis drives your own Chrome | `winget install Google.Chrome` |
| Ollama | Runs the vision model locally | `winget install Ollama.Ollama` |
| A GPU (optional) | Cuts vision answers from ~20 s to 1–3 s | Ollama uses it automatically |

Access to the two private repos: ask the owner to add your GitHub account to the **Rift-FI** organisation.

Get the code, all three folders side by side:

```bash
mkdir rift && cd rift
git clone https://github.com/amschel99/Iriis-voice.git iriis-voice
git clone https://github.com/Rift-FI/Seryn-twin.git seryn-twin
git clone https://github.com/Rift-FI/iriis-visual.git seryn-vision
```

**Secrets go in a `.env` file inside each folder.** Git ignores those files, so never commit them, never paste them in chats, and use different keys from the owner's.

---

## 2. Seryn Vision (bench camera assistant)

### Install (one command)

Windows, in PowerShell inside `seryn-vision`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

On macOS or Linux, run `bash scripts/setup.sh` instead.

The script:
- installs Ollama if it's missing and downloads the vision model (`qwen3.5:2b`, about 3 GB);
- creates `.venv` and installs everything (CPU-only PyTorch);
- downloads the OCR, object-detection, embedding and speech models once;
- creates `.env` from `.env.example`.

### Settings (`seryn-vision/.env`)

| Setting | Needed? | What to put |
|---|---|---|
| `ACCESS_KEY` | Yes, whenever the phone connects over a tunnel or the internet | Any long random password. The phone asks for it once. |
| `VLM_MODEL` | No | `qwen3.5:2b` (default; the most accurate on CPU), `qwen3.5:0.8b` (faster, weaker), or `qwen3.5:4b`/`9b` with a GPU |
| `VLM_FALLBACK` | No | `qwen3.5:0.8b`: used if the main model can't fit in memory |
| `OLLAMA_HOST_URL` | No | `http://127.0.0.1:11434` (default) |
| `DATABASE_URL` | No | Leave empty locally (it uses SQLite in `data/`). Set a Postgres URL to share data between machines. |
| `SERYN_API_KEY` | Optional | A key for the Seryn Twin, so answers can use Seryn's papers and data (see §4) |
| `STT_MODEL` | No | `base.en` (default), `tiny.en` (fastest), `small.en` (most accurate) |
| `DETECTOR` | No | `yolo` (default), `rtdetr` (Apache-2.0; use it if shipping closed-source), `none` |
| `SERYN_MODEL_PATH` | No | Path to your own trained model; plugins/seryn_model.py loads it |
| `MAX_UPLOAD_MB` | No | `200` |

`.env.example` explains every other setting. The defaults are fine.

### Run it

```bash
.venv\Scripts\python -m app        # macOS/Linux: .venv/bin/python -m app
```

It prints the addresses to use and a QR code. Leave it running.

### Connect the phone (the camera needs a secure address)

| Way | Steps | Best for |
|---|---|---|
| **Tunnel** (any network, easiest) | `scripts\tunnel.ps1` (or `.sh`). It prints a `https://…trycloudflare.com` link to open on the phone. `ACCESS_KEY` must be set. | Testing anywhere, including mobile data |
| **USB** (Android) | Turn on USB debugging (Settings → About → tap "Build number" 7 times → Developer options). Run `scripts\usb.ps1`, then open `http://localhost:8000` on the phone. | Lowest delay on the bench |
| **Same Wi-Fi** | `scripts\make_cert.ps1` once (it explains installing its certificate on the phone), restart, then scan the QR code | Daily bench use without cables |

**Watch from the laptop:** open the same address with `/watch` on the end.

---

## 3. Iriis (voice assistant)

Iriis has a **brain** (`iriis server`), which runs the AI, memory and tools, and a **desktop agent** (`iriis start`), which gives it your computer: files, terminal, apps and your Chrome. Two ways to set it up:

### Option A: use the existing cloud brain (the owner's Railway). Only for the owner, or someone they trust with their account.

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\new_computer.ps1
```

It installs everything, then asks for three values from the iriis-voice service's **Variables** on Railway: `IRIIS_SERVER_URL`, `IRIIS_API_KEY` and `IRIIS_DESKTOP_TOKEN`. It also starts Iriis at login.

### Option B: run your own brain locally. Use this for anyone else; it's fully separate from the owner's.

1. **Install:**
   ```bash
   cd iriis-voice
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python scripts\download_models.py --skip-ollama
   ```
2. **Speech-recognition models** (one-time conversion, in its own environment):
   ```bash
   py -3.12 -m venv .venv-convert
   .venv-convert\Scripts\pip install -r scripts\requirements-convert.txt
   .venv-convert\Scripts\python scripts\convert_models.py --models small.en
   ```
3. **Create `iriis-voice/.env`** with the settings below.
4. **Start the brain** in one terminal: `bin\iriis server`.
5. **Connect this PC to it** in a second terminal:
   - `bin\iriis setup`: enter URL `http://127.0.0.1:8000`, and the same `IRIIS_API_KEY` and `IRIIS_DESKTOP_TOKEN` as in `.env`;
   - then `bin\iriis start` and `bin\iriis status`.
6. **Talk to her:** `bin\iriis ask "hello"`, or say "Hey Jarvis" (the wake word until a custom one is trained).

### Iriis settings (`iriis-voice/.env`)

| Setting | Needed? | What / where to get it |
|---|---|---|
| `DEEPSEEK_API_KEY` | **Yes** (the AI model) | platform.deepseek.com → API keys. The model is chosen in `config/config.yaml` (`llm:`). For Groq instead, set `base_url`/`model` as the comment there shows and use `GROQ_API_KEY`. |
| `IRIIS_API_KEY` | Yes | Any long random string: the key apps and the CLI use. `iriis setup` can generate one. |
| `IRIIS_DESKTOP_TOKEN` | Yes | Any long random string: the secret between the desktop agent and the brain |
| `IRIIS_SERVER_URL` | Yes (desktop) | `http://127.0.0.1:8000` locally, or the Railway URL. Written by `iriis setup`. |
| `DATABASE_URL` | No | Empty means SQLite in `data/`. A Postgres URL keeps memories, notes, tasks and money records off the machine. |
| `PLAYWRIGHT_MCP_EXTENSION_TOKEN` | For browser control | Install the "Playwright Extension" in your Chrome, open it and copy its token |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Optional | Chat with Iriis on Telegram. Create a bot with @BotFather, message it, and it replies with your chat id. |
| `TAVILY_API_KEY` / `BRAVE_API_KEY` | Optional | Free search keys (tavily.com has no card). Search works without them. |
| `SERYN_API_KEY` | Optional | Turns on Seryn tools (see §4). |
| `VISION_MCP_URL` + `VISION_ACCESS_KEY` | Optional | Turns on the bench camera tools: `http://localhost:8000/mcp` (or the vision app's Railway URL + `/mcp`) and that app's `ACCESS_KEY`. If the brain is in the cloud, the vision app needs a public address too. |
| `ORPHEUS_TOKEN` | Optional | Only for the self-hosted Orpheus voice GPU. Without it Iriis uses the built-in local voices. |
| `GROQ_API_KEY` / `HF_TOKEN` | Optional | Only if you switch the model or voice to Groq or a protected Hugging Face endpoint |

**Gmail and Calendar** (optional, free): follow README §4b to create an OAuth client, then run `bin\iriis google-login path\to\client_secret.json`.

---

## 4. Seryn Twin (digital twin)

```bash
cd seryn-twin
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt -e .
.venv\Scripts\streamlit run app/Dashboard.py          # the app: http://localhost:8501
```

To let Iriis or Seryn Vision use the twin, also run its gateway, which provides the API and MCP on one port:

```powershell
$env:SERYN_API_KEYS = "pick-a-long-key"; $env:PORT = "8080"     # PowerShell (macOS/Linux: export SERYN_API_KEYS=... PORT=8080)
.venv\Scripts\python -m seryn_twin.gateway                       # http://localhost:8080/mcp and /api/docs
```

Then point the others at it:
- **Seryn Vision:** set `SERYN_API_KEY=pick-a-long-key` and change the `url:` in `seryn-vision/sources.yaml` to `http://localhost:8080/mcp`.
- **Iriis:** set `SERYN_API_KEY=pick-a-long-key` and change the `seryn:` url in `iriis-voice/config/mcp_servers.yaml` to `http://localhost:8080/mcp`.

### Seryn Twin settings

| Setting | Needed? | What |
|---|---|---|
| `SERYN_APP_PASSWORD` | Required on a server; optional locally | The password for the app's login page |
| `SERYN_DATABASE_URL` | Required on Railway; optional locally | Postgres for the research queue and confirmations. Locally, files in the folder are used. |
| `SERYN_API_KEYS` | For the gateway | Comma-separated keys for `/mcp` and `/api`, one per agent or person |
| `OPENALEX_API_KEY`, `S2_API_KEY`, `SERYN_CONTACT_EMAIL` | Optional | Better rate limits for the paper and research search |
| `OPS_KEY` + `OPS_SECRET`, `LENS_TOKEN` | Optional | Live patent search (EPO OPS, Lens.org; both have free tiers) |
| `OLLAMA_URL`, `SERYN_TUTOR_MODEL` | Optional | The spoken tutor in Learn mode (for example `llama3.2` via Ollama) |
| `SERYN_OFFLINE=1` | Optional | Never call the internet |

---

## 5. Day-to-day use

### Seryn Vision (on the phone)

1. **Open and log in:** open the address, enter the access key once, allow the camera (and the microphone for voice).
2. **Set the goal:** type what you're doing in Goal → Set, or pick a **procedure** and press Start.
3. **🎙 Hands-free:** tap it and talk normally while you work, for example "what does the thermometer say?" or "is this the right resistor?". It says "Let me look", then answers out loud. Talk over it to interrupt.
4. **🧭 Coach me:** it watches and speaks up only when something's wrong, out of range or done.
5. **Readings:** these show live on screen (top right) and in the chart. Out-of-range values turn orange and are spoken.
6. **Steps:** use **Check step** and **Next/Back**, or turn on auto-check. A step advances after two good checks.
7. **📷 Snap / ⏺ Record:** recordings keep your voice. In *Photos & videos*, tap "what happened?" for a timeline review.
8. **⏪ What did I just do?** reviews the last 90 seconds.
9. **Knowledge & data:** upload manuals, datasheets or notes, or paste links. Answers use them and name the source. You can also import a Seryn protocol (e.g. `P2`) as a procedure.
10. **Watch screen:** the laptop at `…/watch` shows live video, readings and answers, and can ask questions too.

### Iriis

- **The app (easiest):** open the Iriis address on your phone, enter the key once, and install it (Android: Install; iPhone: Share → Add to Home Screen).
  - Type, or hold 🎤 to talk; double-tap 🎤 for hands-free.
  - Tap **Yes / No / Yes to all** when she asks before acting.
  - ☰ → **Tasks** shows background jobs and routines; ☰ → **Settings** turns on notifications; ☰ → **Guide** walks you through everything.
- **Voice:** say "Hey Jarvis…". In a terminal, use `iriis ask "…"`. On Telegram, message the bot (text or voice notes).
- **Example requests:**
  - "Look into X and report back" (a background task)
  - "Every weekday at 7, brief me" (a routine)
  - "What's open in Brave?"
  - "Read my latest WhatsApp messages"
  - "Chart my spending this month and send it to Telegram"
  - "What's on my bench right now?" (needs the bench connection, §3)
- **Safety:** anything risky (sending, deleting, buying, running changes) asks first in plain words. Answer **yes**, **no**, or **yes to all** (no more questions for that kind of action for 10 minutes).
- **Checks:** `iriis status` shows whether she's connected; `iriis restart` after changing settings.

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| The phone says the camera needs a secure page | Use the tunnel, USB or Wi-Fi with a certificate (§2). A plain `http://192.168…` address can't use the camera. |
| "Model isn't pulled" | `ollama pull qwen3.5:2b` |
| Answers are very slow | Normal on CPU (20–40 s). Use a GPU, `VLM_MODEL=qwen3.5:0.8b`, or 480p. |
| The watch page shows "relayed pictures" | The network blocks direct video; it still works at about 4 pictures/second |
| Iriis: "not connected to my brain" | Check `iriis status`, the URL and keys in `.env`, and that the brain is running |
| Iriis can't use the browser | Chrome must be open with the Playwright Extension, and its token must be in `.env` (then `iriis restart`) |
| PaddleOCR "ConvertPirAttribute" error on Windows | Keep `PADDLE_MKLDNN=0` (the default) |

More detail is in each repo's README.
