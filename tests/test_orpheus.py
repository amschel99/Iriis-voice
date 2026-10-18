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
        async def synthesize(self, text, voice=None):
            self.calls.append((text, voice))
            return np.zeros(10, dtype=np.float32), 24000

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
    cfg.tts.voice_casting["troy"] = "male; very deep and slow: kings, villains"
    p = build_system_prompt("Iriis", "", "Nairobi", "Africa/Nairobi", casting=cfg.tts.voice_casting)
    assert "{troy}: male; very deep and slow: kings, villains" in p
    assert "DIFFERENT voice" in p and "keep the same" in p     # the casting rules
