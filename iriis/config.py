"""Configuration loading and validation (pydantic)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"
DEFAULT_MCP_CONFIG = ROOT / "config" / "mcp_servers.yaml"

Tier = Literal["auto", "confirm", "blocked"]


class AudioConfig(BaseModel):
    input_device: int | str | None = None
    output_device: int | str | None = None
    sample_rate: int = 16000
    block_ms: int = 80  # 1280 samples at 16 kHz, what openWakeWord expects
    chime: bool = True


class WakeWordConfig(BaseModel):
    mode: Literal["wakeword", "push_to_talk", "always"] = "wakeword"
    model: str = "hey_jarvis"
    threshold: float = 0.5
    push_to_talk_key: str = "f9"
    followup_s: float = 86400.0  # conversation mode: after she answers, keep listening this long (24 h; no wake word)


class VADConfig(BaseModel):
    threshold: float = 0.4
    silence_ms: int = 1200
    min_speech_ms: int = 250
    max_utterance_s: float = 20.0
    no_speech_timeout_s: float = 8.0


class STTConfig(BaseModel):
    backend: Literal["faster_whisper", "parakeet"] = "faster_whisper"
    model: str = "models/stt/faster-whisper-small.en"
    parakeet_dir: str = "models/stt/parakeet-tdt-0.6b-v2-int8"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: str = "auto"
    beam_size: int = 1
    cpu_threads: int = 0
    vocabulary: list[str] = Field(default_factory=list)
    # Feed `vocabulary` to Whisper as an initial prompt. OFF by default: the AfriSpeech fine-tunes
    # never saw prompts in training and a prompt can wreck their output (measured: "7." instead of a sentence).
    use_vocabulary_prompt: bool = False


class LLMConfig(BaseModel):
    # openai = any OpenAI-compatible API (Groq, OpenRouter, OpenAI, llama.cpp server, Ollama's /v1)
    backend: Literal["ollama", "llamacpp", "openai"] = "openai"
    host: str = "http://127.0.0.1:11434"
    base_url: str = "https://api.groq.com/openai/v1"   # used by backend: openai
    api_key_env: str = "GROQ_API_KEY"                   # name of the env var holding the key
    reasoning_effort: str | None = "low"                 # gpt-oss on Groq: low = fastest first token
    # DeepSeek "thinking": off = answers immediately (best for voice); low/high = reasons first (slower, deeper)
    thinking: Literal["off", "low", "high"] = "off"
    max_tokens: int = 2048
    model: str = "openai/gpt-oss-120b"
    fallback_model: str = "openai/gpt-oss-20b"
    offline_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    use_fallback_below_free_gb: float = 7.0
    temperature: float = 0.4
    num_ctx: int = 8192
    keep_alive: str = "30m"
    think: bool = False
    max_tool_iterations: int = 6
    history_max_tokens: int = 5000
    request_timeout_s: float = 120.0


DEFAULT_CASTING = {
    # Defaults are first guesses (nobody has listened yet): run `iriis voices` and describe what you HEAR.
    "groq": {
        "hannah": "female; Iriis' own voice. Narrator, and herself in every non-story reply",
        "diana": "female; mature, composed: mothers, teachers, bosses, officials, the wise elder",
        "autumn": "female; youthful, bright: girls, best friends, excited or nervous young women",
        "troy": "male; deep, heavy: villains, guards, fathers, big tough men, menace",
        "austin": "male; young, casual: boys, students, friends, the funny sidekick",
        "daniel": "male; calm, measured: professionals, doctors, news readers, old wise men",
    },
    "open": {
        "tara": "female; Iriis' own voice (the most natural Orpheus voice). Narrator, and herself in every non-story reply",
        "leah": "female; mature, warm: mothers, teachers, bosses, the wise elder",
        "jess": "female; lively, casual: best friends, gossip, sassy characters",
        "mia": "female; young, bright: girls, students, nervous or excited young women",
        "zoe": "female; soft, gentle: shy or kind characters, comforting moments",
        "leo": "male; deep, steady: villains, guards, fathers, big tough men",
        "dan": "male; casual, friendly: friends, the funny sidekick, everyday men",
        "zac": "male; young, energetic: boys, students, excitable characters",
    },
}


class TTSConfig(BaseModel):
    # orpheus = expressive. Falls back to kokoro, then piper, whenever it is unavailable.
    backend: Literal["orpheus", "kokoro", "piper"] = "kokoro"
    # groq = hosted on Groq (needs GROQ_API_KEY; [angry]-style directions)
    # open = self-hosted open-source Orpheus on a Modal GPU (deploy/orpheus_modal.py; <laugh>-style sounds)
    orpheus_flavor: Literal["groq", "open"] = "groq"
    orpheus_url: str = ""                  # open: the Modal URL from `modal deploy` (groq: leave empty)
    orpheus_model: str = "canopylabs/orpheus-v1-english"
    orpheus_voice: str = ""                # her own voice; default hannah (groq) / tara (open)
    orpheus_api_key_env: str = "GROQ_API_KEY"
    orpheus_token_env: str = "ORPHEUS_TOKEN"   # open: the ORPHEUS_TOKEN your voice server was given
    orpheus_host_token_env: str = ""       # open + protected Hugging Face endpoint: e.g. "HF_TOKEN"
    # Casting sheet per flavour: how each voice sounds and what it suits; the AI casts characters from it.
    voice_casting: dict[str, dict[str, str]] = Field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_CASTING.items()})

    def effective_flavor(self) -> str | None:
        """The Orpheus flavour that can actually run: your own GPU if configured, else Groq if there's a key."""
        import os
        if self.backend != "orpheus":
            return None
        own = bool(self.orpheus_url and os.environ.get(self.orpheus_token_env))
        groq = bool(os.environ.get(self.orpheus_api_key_env))
        if self.orpheus_flavor == "open":
            return "open" if own else ("groq" if groq else None)
        return "groq" if groq else ("open" if own else None)

    def casting(self) -> dict[str, str]:
        flavor = self.effective_flavor() or self.orpheus_flavor
        return self.voice_casting.get(flavor) or DEFAULT_CASTING[flavor]
    kokoro_model: str = "models/tts/kokoro-v1.0.int8.onnx"
    kokoro_voices: str = "models/tts/voices-v1.0.bin"
    voice: str = "af_heart"
    piper_model: str = "models/tts/en_GB-alba-medium.onnx"
    speed: float = 1.0
    volume: float = 1.0
    currency: str = "shillings"


class BargeInConfig(BaseModel):
    enabled: bool = True
    min_speech_ms: int = 500
    # Mic energy must exceed (recent) playback energy * this ratio to count as the user (echo gate).
    # Laptop speakers + built-in mic need ~2.5; with headphones you can drop it to 1.2.
    echo_gate_ratio: float = 2.5
    stop_words: list[str] = Field(default_factory=lambda: ["stop", "cancel", "never mind", "nevermind", "shut up", "quiet"])


class PermissionsConfig(BaseModel):
    default_tier: Tier = "confirm"
    # Per-tool overrides, keyed by namespaced tool name ("server__tool") or glob ("files__write_*").
    overrides: dict[str, Tier] = Field(default_factory=dict)
    dry_run: bool = False
    audit_log: str = "logs/audit.jsonl"
    confirm_words: list[str] = Field(default_factory=lambda: ["yes", "yeah", "yep", "confirm", "confirmed", "do it", "go ahead", "sure", "okay", "ok", "proceed"])


class ToolRoutingConfig(BaseModel):
    max_tools: int = 40
    method: Literal["keyword", "llm"] = "keyword"


class MemoryConfig(BaseModel):
    # Storage is chosen by the environment: DATABASE_URL (Postgres, e.g. Railway) or else
    # SQLite at IRIIS_SQLITE_PATH (default data/iriis.sqlite3). See iriis/storage.py.
    enabled: bool = True
    # Conversations are buffered in memory and written to the database in one batch this often
    # (also on shutdown, so redeploys lose nothing). Lower = less lost if the process crashes.
    flush_interval_s: float = 120.0


class FilesConfig(BaseModel):
    allowed_dirs: list[str] = Field(default_factory=lambda: ["~/Documents", "~/Desktop", "~/Downloads"])


class UIConfig(BaseModel):
    show_latency: bool = True


class ServerConfig(BaseModel):
    """The cloud core (python -m iriis.server)."""
    host: str = "0.0.0.0"
    port: int = 8000                                   # Railway's $PORT overrides this
    api_key_env: str = "IRIIS_API_KEY"                # clients: Authorization: Bearer <key>
    desktop_token_env: str = "IRIIS_DESKTOP_TOKEN"    # the desktop agent's separate secret
    # Built-in MCP servers that run inside the cloud container (the rest run on the desktop).
    cloud_servers: list[str] = Field(default_factory=lambda: ["time_and_calc", "memory", "notes_reminders"])
    stt: bool = True                                   # load the STT model (voice + /v1/stt)
    tts: bool = True                                   # load the TTS model (spoken replies)
    confirm_timeout_s: float = 60.0
    max_sessions: int = 200


class DesktopConfig(BaseModel):
    """The desktop agent (python -m iriis.desktop)."""
    server_url: str = "ws://127.0.0.1:8000"            # e.g. wss://your-app.up.railway.app
    token_env: str = "IRIIS_DESKTOP_TOKEN"
    api_key_env: str = "IRIIS_API_KEY"                # used by the voice client
    servers: list[str] = Field(default_factory=lambda: ["system_info", "files", "shell", "apps_and_media", "clipboard",
                                                         "web", "browser"])
    # cloud: the cloud asks you to confirm, and the desktop only runs confirm-tier tools when the
    #        call says you confirmed. local: the desktop also asks in its own terminal (safest).
    confirm_mode: Literal["cloud", "local"] = "cloud"


class Config(BaseModel):
    assistant_name: str = "Iriis"
    user_name: str = ""
    location: str = "Nairobi, Kenya"
    timezone: str = "Africa/Nairobi"
    audio: AudioConfig = AudioConfig()
    wakeword: WakeWordConfig = WakeWordConfig()
    vad: VADConfig = VADConfig()
    stt: STTConfig = STTConfig()
    llm: LLMConfig = LLMConfig()
    tts: TTSConfig = TTSConfig()
    barge_in: BargeInConfig = BargeInConfig()
    permissions: PermissionsConfig = PermissionsConfig()
    tool_routing: ToolRoutingConfig = ToolRoutingConfig()
    memory: MemoryConfig = MemoryConfig()
    files: FilesConfig = FilesConfig()
    ui: UIConfig = UIConfig()
    server: ServerConfig = ServerConfig()
    desktop: DesktopConfig = DesktopConfig()
    mcp_config: str = "config/mcp_servers.yaml"


class MCPServerConfig(BaseModel):
    name: str
    enabled: bool = True
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    tier: Tier = "confirm"
    tools: dict[str, Tier] = Field(default_factory=dict)  # per-tool tier overrides
    exclude: list[str] = Field(default_factory=list)      # tools (globs) never shown to the AI
    description: str = ""
    keywords: list[str] = Field(default_factory=list)  # used by the keyword tool router
    needs_internet: bool = False


def resolve_path(p: str | os.PathLike) -> Path:
    """Resolve a config path relative to the project root (with ~ expansion)."""
    path = Path(os.path.expandvars(os.path.expanduser(str(p))))
    return path if path.is_absolute() else ROOT / path


def _env_overrides(data: dict) -> dict:
    """IRIIS__LLM__MODEL=x sets data["llm"]["model"] = x (values parsed as YAML: numbers, lists...)."""
    for key, value in os.environ.items():
        if not key.startswith("IRIIS__"):
            continue
        parts = [p.lower() for p in key[len("IRIIS__"):].split("__") if p]
        node = data
        for p in parts[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = yaml.safe_load(value) if value.strip() else value
    return data


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("IRIIS_CONFIG") or DEFAULT_CONFIG)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(_env_overrides(data))


def load_mcp_servers(path: str | os.PathLike | None = None) -> list[MCPServerConfig]:
    path = resolve_path(path) if path else DEFAULT_MCP_CONFIG
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    servers = data.get("mcpServers", {}) or {}
    return [MCPServerConfig.model_validate({"name": name, **(spec or {})}) for name, spec in servers.items()]


def apply_offline_env() -> None:
    """Force every library we use into offline / no-telemetry mode. Call before heavy imports."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    os.environ.setdefault("OLLAMA_NO_CLOUD", "1")
