"""Expressive voice (Orpheus via Groq): directions, splitting, and falling back when Groq fails."""
import io

import numpy as np
import pytest

from iriis.config import Config
from iriis.tts.orpheus_engine import MAX_CHARS, OrpheusEngine, split_for_orpheus
from iriis.tts.text_normalize import normalize, strip_directions


def test_directions_kept_for_orpheus_removed_elsewhere():
    t = "[angry] Close the laptop. It costs KSh 1,500!"
    assert normalize(t, keep_directions=True).startswith("[angry] Close the laptop")
    assert "angry" not in normalize(t) and "fifteen hundred shillings" in normalize(t)
    assert strip_directions(t) == "Close the laptop. It costs KSh 1,500!"


def test_all_caps_rant_becomes_a_shout():
    assert normalize("BRO. SIT THE FUCK DOWN.", keep_directions=True) == "[shouting] bro. sit the fuck down."


def test_split_respects_limit_and_carries_emotion():
    pieces = split_for_orpheus("[angry] " + "You are one person, one person, " * 12 + "Go to bed. Now.")
    assert all(len(p) <= MAX_CHARS for p in pieces)
    assert all(p.startswith("[angry]") for p in pieces)
    assert "".join(p.replace("[angry] ", "") for p in pieces).count("one person") == 24


def _wav(seconds=0.2, sr=24000) -> bytes:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(sr * seconds), dtype=np.float32), sr, format="WAV")
    return buf.getvalue()


class FakeResp:
    def __init__(self, status, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


def test_engine_calls_groq_per_piece(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    eng = OrpheusEngine(Config().tts)
    sent = []
    monkeypatch.setattr(eng.http, "post", lambda url, headers, json: sent.append(json) or FakeResp(200, _wav()))
    audio = eng.synthesize("[warm] Hello there. " + "Long sentence, " * 20 + "done.")
    assert len(sent) >= 2 and all(len(j["input"]) <= MAX_CHARS for j in sent)
    assert sent[0]["voice"] == "hannah" and sent[0]["model"] == "canopylabs/orpheus-v1-english"
    assert audio.dtype == np.float32 and len(audio) > 0


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        OrpheusEngine(Config().tts)


def test_server_falls_back_to_kokoro_when_groq_fails(monkeypatch):
    from iriis.server.speech import Speech
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    cfg = Config()
    cfg.tts.backend = "orpheus"
    speech = Speech(cfg)
    eng = speech._load_tts()
    assert eng.name.startswith("orpheus")
    monkeypatch.setattr(eng.http, "post", lambda *a, **k: FakeResp(429, text="rate limited"))

    class FakeKokoro:
        name, sample_rate = "kokoro:af_heart", 24000

        def synthesize(self, text):
            assert "[" not in text           # the fallback never reads directions aloud
            return np.ones(100, dtype=np.float32)
    speech._fallback = FakeKokoro()
    audio, sr = speech._synthesize("[angry] Go to bed.", None)
    assert len(audio) == 100 and sr == 24000


def test_ai_chooses_voice_and_emotion_per_sentence():
    from iriis.tts.text_normalize import Delivery, take_voice
    assert take_voice("{Troy} Hello") == ("troy", "Hello")
    d = Delivery()
    assert d.apply("Once upon a time.") == ("Once upon a time.", None)
    assert d.apply("{troy} [gravelly] Who goes there?") == ("[gravelly] Who goes there?", "troy")
    assert d.apply("I said who goes there!") == ("[gravelly] I said who goes there!", "troy")   # carries on
    assert d.apply("{hannah} It was the cat.") == ("It was the cat.", "hannah")                # new voice, neutral
    assert strip_directions("{troy} [angry] Who?") == "Who?"
    assert "troy" not in normalize("{troy} [angry] Who goes there?")   # calm voices never read tags


def test_turn_passes_chosen_voices_to_tts(tmp_path, monkeypatch):
    import asyncio
    from iriis.agent.loop import Agent
    from iriis.agent.permissions import PermissionManager
    from iriis.llm.base import LLMClient, LLMEvent
    from iriis.server.sessions import Session, run_turn

    class StoryLLM(LLMClient):
        model = "fake"
        async def chat_stream(self, messages, tools=None, model=None):
            for t in ["[warm] Here's a quick one. ", "{troy} [gravelly] Who goes there? ", "{hannah} [laughing] Just the cat!"]:
                yield LLMEvent(text=t)
            yield LLMEvent(done=True)
        async def health(self):
            return True, ""

    class FakeSpeech:
        calls = []
        async def stream(self, text, voice=None):
            self.calls.append((text, voice))
            yield np.zeros(10, dtype=np.float32), 24000

    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    agent = Agent(cfg, StoryLLM(), None, PermissionManager(cfg, []))
    events = []

    async def emit(ev, audio):
        events.append(ev)

    speech = FakeSpeech()
    asyncio.run(run_turn(Session("s", agent), "tell me a story", emit, speech=speech, speak=True))
    assert [v for _, v in speech.calls] == [None, "troy", "hannah"]
    assert speech.calls[1][0].startswith("[gravelly]")
    shown = [e["text"] for e in events if e["type"] == "sentence"]
    assert shown == ["Here's a quick one.", "Who goes there?", "Just the cat!"]


def test_casting_sheet_from_config_reaches_the_prompt():
    from iriis.llm.prompts import build_system_prompt
    cfg = Config()
    cfg.tts.orpheus_flavor = "groq"
    cfg.tts.voice_casting["groq"]["troy"] = "male; very deep and slow: kings, villains"
    p = build_system_prompt("Iriis", "", "Nairobi", "Africa/Nairobi", casting=cfg.tts.casting(), voice_style="groq")
    assert "{troy}: male; very deep and slow: kings, villains" in p
    assert "DIFFERENT voice" in p and "keep the same" in p     # the casting rules


def _open_engine(monkeypatch):
    monkeypatch.setenv("ORPHEUS_TOKEN", "ot")
    cfg = Config()
    cfg.tts.backend, cfg.tts.orpheus_flavor, cfg.tts.orpheus_url = "orpheus", "open", "https://me--iriis-orpheus.modal.run"
    return OrpheusEngine(cfg.tts), cfg


def test_open_flavor_needs_url_and_token(monkeypatch):
    cfg = Config()
    cfg.tts.orpheus_flavor = "open"
    with pytest.raises(RuntimeError, match="orpheus_url"):
        OrpheusEngine(cfg.tts)
    cfg.tts.orpheus_url = "https://x.modal.run"
    monkeypatch.delenv("ORPHEUS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ORPHEUS_TOKEN"):
        OrpheusEngine(cfg.tts)


def test_cold_gpu_wakes_in_background_and_kokoro_covers(monkeypatch):
    eng, cfg = _open_engine(monkeypatch)
    assert eng.voice == "tara" and eng.name == "orpheus-open:tara"
    health_calls, posts = [], []
    monkeypatch.setattr(eng.http, "get", lambda url, headers=None, timeout=None: health_calls.append(url) or FakeResp(200))
    monkeypatch.setattr(eng.http, "post", lambda url, headers, json: posts.append((url, json)) or FakeResp(200, _wav()))
    with pytest.raises(RuntimeError, match="waking up"):
        eng.synthesize("Hello there.")            # cold: refuse quickly, start the warm-up
    eng._warming.join(2)
    assert health_calls == ["https://me--iriis-orpheus.modal.run/health"] and eng.warm
    eng.synthesize("<laugh> Hello there.", voice="hannah")   # warm now; hannah maps to tara
    url, body = posts[0]
    assert url.endswith("/v1/audio/speech") and body["voice"] == "tara" and body["input"] == "<laugh> Hello there."


def test_server_uses_open_style_text_and_falls_back_when_cold(monkeypatch):
    from iriis.server.speech import Speech
    eng, cfg = _open_engine(monkeypatch)
    speech = Speech(cfg)
    speech._tts = eng
    monkeypatch.setattr(eng.http, "get", lambda url, headers=None, timeout=None: FakeResp(503))   # never warms here
    monkeypatch.setattr("iriis.tts.orpheus_engine.WARM_POLL_S", 0.01)

    class FakeKokoro:
        name, sample_rate = "kokoro:af_heart", 24000
        def synthesize(self, text):
            assert "<" not in text and "[" not in text
            return np.ones(50, dtype=np.float32)
    speech._fallback = FakeKokoro()
    audio, _ = speech._synthesize("[laughing] You again? <sigh> Fine.", None)
    assert len(audio) == 50                                  # cold GPU: Kokoro spoke instead of silence
    assert normalize("[laughing] You again? <sigh> Fine.", style="open") == "<laugh> You again? <sigh> Fine."


def test_prompt_style_follows_what_is_usable(monkeypatch, tmp_path):
    from iriis.agent.loop import Agent
    from iriis.agent.permissions import PermissionManager
    cfg = Config()
    cfg.tts.backend, cfg.tts.orpheus_flavor = "orpheus", "open"
    agent = Agent(cfg, None, None, PermissionManager(cfg, []))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ORPHEUS_TOKEN", raising=False)
    assert agent._voice_style() == "plain"                    # nothing usable: don't write any tags
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    assert agent._voice_style() == "groq"                     # no GPU yet, but Groq works meanwhile
    assert "hannah" in cfg.tts.casting()
    cfg.tts.orpheus_url = "https://x.modal.run"
    monkeypatch.setenv("ORPHEUS_TOKEN", "ot")
    assert agent._voice_style() == "open"                     # own GPU wins once it exists
    assert "tara" in cfg.tts.casting()


def test_warm_up_keeps_polling_a_booting_host(monkeypatch):
    eng, _ = _open_engine(monkeypatch)
    monkeypatch.setattr("iriis.tts.orpheus_engine.WARM_POLL_S", 0.01)
    answers = iter([FakeResp(502), FakeResp(503), FakeResp(503), FakeResp(200)])
    monkeypatch.setattr(eng.http, "get", lambda url, headers=None, timeout=None: next(answers))
    eng.warm_up(wait=True)
    assert eng.warm


def test_protected_host_gets_its_token_and_ours_moves_to_a_header(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("ORPHEUS_TOKEN", "ot")
    cfg = Config()
    cfg.tts.orpheus_flavor, cfg.tts.orpheus_url, cfg.tts.orpheus_host_token_env = "open", "https://e.endpoints.huggingface.cloud", "HF_TOKEN"
    eng = OrpheusEngine(cfg.tts)
    assert eng.headers == {"Authorization": "Bearer hf_x", "X-Orpheus-Token": "ot"}


def test_open_flavor_sends_token_in_header_hf_does_not_strip(monkeypatch):
    eng, _ = _open_engine(monkeypatch)
    assert eng.headers["X-Orpheus-Token"] == "ot"



def test_streamed_audio_stays_in_order_and_flags_last_chunk(tmp_path):
    """Sentence 2 is prefetched and may finish first; the listener must still hear 1 then 2."""
    import asyncio
    from iriis.agent.loop import Agent
    from iriis.agent.permissions import PermissionManager
    from iriis.llm.base import LLMClient, LLMEvent
    from iriis.server.sessions import Session, run_turn

    class TwoSentences(LLMClient):
        model = "fake"
        async def chat_stream(self, messages, tools=None, model=None):
            yield LLMEvent(text="First sentence is slow. ")
            yield LLMEvent(text="Second one is quick.")
            yield LLMEvent(done=True)
        async def health(self):
            return True, ""

    class SlowFirstSpeech:
        async def stream(self, text, voice=None):
            n, delay = (3, 0.05) if text.startswith("First") else (2, 0.0)
            for i in range(n):
                await asyncio.sleep(delay)
                yield np.full(4, i, dtype=np.float32), 24000

    cfg = Config()
    cfg.permissions.audit_log = str(tmp_path / "a.jsonl")
    agent = Agent(cfg, TwoSentences(), None, PermissionManager(cfg, []))
    audio_events = []

    async def emit(ev, data):
        if ev["type"] == "audio":
            audio_events.append((ev["text"], ev["last"], ev["seq"]))

    asyncio.run(run_turn(Session("s", agent), "go", emit, speech=SlowFirstSpeech(), speak=True))
    texts = [t for t, _, _ in audio_events]
    assert texts == ["First sentence is slow."] * 3 + ["Second one is quick."] * 2
    assert [l for _, l, _ in audio_events] == [False, False, True, False, True]
    assert [q for _, _, q in audio_events] == [1, 2, 3, 4, 5]


def test_speech_stream_falls_back_to_kokoro_when_gpu_is_cold(monkeypatch):
    import asyncio
    from iriis.server.speech import Speech
    eng, cfg = _open_engine(monkeypatch)
    monkeypatch.setattr(eng.http, "get", lambda url, headers=None, timeout=None: FakeResp(503))
    monkeypatch.setattr("iriis.tts.orpheus_engine.WARM_POLL_S", 0.01)
    speech = Speech(cfg)
    speech._tts = eng

    class FakeKokoro:
        name, sample_rate = "kokoro:af_heart", 24000
        def synthesize(self, text):
            return np.ones(30, dtype=np.float32)
    speech._fallback = FakeKokoro()

    async def collect():
        return [a async for a in speech.stream("<laugh> Hello there.")]
    chunks = asyncio.run(collect())
    assert len(chunks) == 1 and len(chunks[0][0]) == 30      # whole sentence from Kokoro, no silence
