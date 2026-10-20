"""Server-side STT and TTS, loaded once on first use and run in worker threads."""
from __future__ import annotations

import asyncio
import io
import threading

import numpy as np

from iriis.config import Config
from iriis.tts.text_normalize import normalize, take_voice


def pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def from_pcm16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def decode_audio_file(data: bytes, name: str = "", mime: str = "") -> np.ndarray:
    """Any recording -> 16 kHz mono float32. WAV/FLAC via soundfile; what phones and browsers actually send
    (WebM/Opus from Chrome and Android, MP4/AAC from iPhone Safari, Ogg from Firefox and Telegram) via PyAV."""
    from iriis.audio.playback import resample
    try:
        import soundfile as sf
        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        return resample(audio.mean(axis=1), sr, 16000)
    except Exception:  # noqa: BLE001  (not a format libsndfile knows: decode with FFmpeg through PyAV)
        pass
    import av
    chunks = []
    hint = next((f for k, f in (("webm", "webm"), ("ogg", "ogg"), ("mp4", "mp4"), ("m4a", "mp4"), ("aac", "aac"),
                                ("mpeg", "mp3"), ("mp3", "mp3"), ("wav", "wav")) if k in f"{mime} {name}".lower()), None)
    try:
        c = av.open(io.BytesIO(data), mode="r")
    except Exception:  # noqa: BLE001  (some phone recordings need the container named explicitly)
        if not hint:
            raise
        c = av.open(io.BytesIO(data), mode="r", format=hint)
    with c:
        stream = next((s for s in c.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio in that recording")
        res = av.AudioResampler(format="flt", layout="mono", rate=16000)
        for frame in c.decode(stream):
            for f in res.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1))
        for f in res.resample(None):
            chunks.append(f.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("the recording is empty")
    return np.concatenate(chunks).astype(np.float32)


class Speech:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._stt = None
        self._tts = None
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._fallback = None

    # ------------------------------------------------------------------ loading

    def _load_stt(self):
        with self._stt_lock:
            if self._stt is None:
                from iriis.stt import make_stt
                self._stt = make_stt(self.cfg.stt)
                self._stt.warmup()
        return self._stt

    def _load_tts(self):
        with self._tts_lock:
            if self._tts is None:
                from iriis.tts import make_tts
                chain = [self.cfg.tts.backend] + [b for b in ("kokoro", "piper") if b != self.cfg.tts.backend]
                flavor = self.cfg.tts.effective_flavor()
                if flavor:   # own GPU if configured, else Groq's Orpheus if there's a key
                    self.cfg.tts.orpheus_flavor = flavor
                errors = []
                for backend in chain:
                    if backend == "orpheus" and not flavor:
                        errors.append("orpheus: no GPU URL/token and no GROQ_API_KEY")
                        continue
                    try:
                        self._tts = make_tts(self.cfg.tts, backend)
                        break
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{backend}: {e}")
                if self._tts is None:
                    raise RuntimeError("; ".join(errors))
        return self._tts

    def _offline_voice(self):
        """Kokoro (or Piper) on the server's own CPU, used if the expressive voice fails mid-conversation."""
        if self._fallback is None:
            from iriis.tts import make_tts
            try:
                self._fallback = make_tts(self.cfg.tts, "kokoro")
            except Exception:  # noqa: BLE001
                self._fallback = make_tts(self.cfg.tts, "piper")
        return self._fallback

    def warm_up(self) -> None:
        """Wake a scale-to-zero voice GPU early (e.g. when a conversation starts)."""
        if self._tts is not None and hasattr(self._tts, "warm_up"):
            self._tts.warm_up()

    async def preload(self) -> list[str]:
        problems = []
        if self.cfg.server.stt:
            try:
                await asyncio.to_thread(self._load_stt)
            except Exception as e:  # noqa: BLE001
                problems.append(f"STT unavailable: {e}")
        if self.cfg.server.tts:
            try:
                await asyncio.to_thread(self._load_tts)
            except Exception as e:  # noqa: BLE001
                problems.append(f"TTS unavailable: {e}")
        return problems

    @property
    def stt_name(self) -> str:
        return self._stt.name if self._stt else "not loaded"

    @property
    def tts_name(self) -> str:
        return self._tts.name if self._tts else "not loaded"

    # ------------------------------------------------------------------ work

    def _transcribe(self, audio: np.ndarray):
        stt = self._load_stt()
        with self._stt_lock:
            return stt.transcribe(audio)

    async def transcribe(self, audio: np.ndarray):
        return await asyncio.to_thread(self._transcribe, audio)

    def _synthesize(self, text: str, voice: str | None) -> tuple[np.ndarray, int]:
        tagged_voice, text = take_voice(text)      # "{troy} ..." picks the voice for this text
        voice = tagged_voice or voice
        tts = self._load_tts()
        if tts.name.startswith("orpheus"):
            spoken = normalize(text, self.cfg.tts.currency, style=tts.style)
            if not spoken:
                return np.zeros(0, dtype=np.float32), tts.sample_rate
            try:
                return tts.synthesize(spoken, voice=voice), tts.sample_rate
            except Exception as e:  # noqa: BLE001  (Groq down / rate limited: keep talking with Kokoro)
                import logging
                logging.getLogger("iriis.speech").warning("expressive voice failed, using fallback: %s", e)
                tts = self._offline_voice()
        spoken = normalize(text, self.cfg.tts.currency)
        if not spoken:
            return np.zeros(0, dtype=np.float32), tts.sample_rate
        with self._tts_lock:
            if voice and hasattr(tts, "cfg") and voice != tts.cfg.voice and voice not in ("hannah", "diana", "autumn", "troy", "austin", "daniel"):
                old = tts.cfg.voice
                tts.cfg.voice = voice
                try:
                    audio = tts.synthesize(spoken)
                finally:
                    tts.cfg.voice = old
            else:
                audio = tts.synthesize(spoken)
        return audio, tts.sample_rate

    async def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        return await asyncio.to_thread(self._synthesize, text, voice)

    async def stream(self, text: str, voice: str | None = None):
        """Async iterator of (audio, sample_rate) chunks, as soon as the voice produces them.
        Streams from the GPU voice when it's warm; otherwise (or if it fails before the first chunk)
        yields the whole sentence once from the fallback path."""
        tagged_voice, plain = take_voice(text)
        use_voice = tagged_voice or voice
        tts = self._load_tts()
        if tts.name.startswith("orpheus") and hasattr(tts, "stream"):
            spoken = normalize(plain, self.cfg.tts.currency, style=tts.style)
            if not spoken:
                return
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            stop = threading.Event()

            def produce():
                try:
                    for chunk in tts.stream(spoken, use_voice, stop=stop):
                        loop.call_soon_threadsafe(q.put_nowait, ("chunk", chunk))
                    loop.call_soon_threadsafe(q.put_nowait, ("end", None))
                except Exception as e:  # noqa: BLE001
                    loop.call_soon_threadsafe(q.put_nowait, ("error", e))

            threading.Thread(target=produce, name="tts-stream", daemon=True).start()
            got_any = False
            try:
                while True:
                    kind, val = await q.get()
                    if kind == "chunk":
                        got_any = True
                        yield val, tts.sample_rate
                    elif kind == "end":
                        return
                    else:
                        if got_any:
                            return   # failed mid-sentence: what was said stands
                        break        # failed before any audio: say it with the fallback voice
            finally:
                stop.set()           # barge-in / cancelled turn: abandon the GPU request
        audio, sr = await self.synthesize(text, voice)
        if len(audio):
            yield audio, sr
