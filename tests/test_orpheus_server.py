"""The GPU voice server's HTTP layer and token decoding, tested without a GPU (fake model)."""
import random
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
from orpheus_server.core import (AUDIO_TOKEN_BASE, END_OF_SPEECH, START_OF_SPEECH,  # noqa: E402
                                 codes_from_token_ids, create_app, split_codebooks, stream_plan)


def test_token_ids_to_codes_matches_reference_math():
    codes = [random.randrange(4096) for _ in range(28)]
    # reference (canopyai decoder): id_N - 10 - (index % 7) * 4096, where token id = 128256 + N
    tids = [START_OF_SPEECH] + [AUDIO_TOKEN_BASE + c + (i % 7) * 4096 for i, c in enumerate(codes)]
    tids += [END_OF_SPEECH, AUDIO_TOKEN_BASE + 5]          # anything after end-of-speech is ignored
    assert codes_from_token_ids(tids) == codes
    assert codes_from_token_ids(tids[:7]) == []             # 6 codes: less than one 7-token frame


def test_frames_split_into_snac_codebooks():
    c0, c1, c2 = split_codebooks(list(range(14)))
    assert c0 == [0, 7] and c1 == [1, 4, 8, 11] and c2 == [2, 3, 5, 6, 9, 10, 12, 13]


class FakeModel:
    def __init__(self):
        self.ready, self.error, self.calls = False, "", []

    def load(self):
        pass

    async def wav(self, text, voice, **kw):
        self.calls.append((text, voice))
        return b"RIFF....WAVE"

    async def stream_pcm(self, text, voice, **kw):
        self.calls.append((text, voice))
        for _ in range(3):
            yield bytes(4096)


def make(monkeypatch):
    monkeypatch.setenv("ORPHEUS_TOKEN", "secret")
    model = FakeModel()
    return TestClient(create_app(model, load_in_background=False)), model


def test_health_is_503_until_loaded(monkeypatch):
    client, model = make(monkeypatch)
    assert client.get("/health").status_code == 503
    model.ready = True
    assert client.get("/health").json()["ok"] is True


def test_speech_needs_token_and_valid_voice(monkeypatch):
    client, model = make(monkeypatch)
    model.ready = True
    body = {"input": "<laugh> Hello", "voice": "tara"}
    assert client.post("/v1/audio/speech", json=body).status_code == 401
    assert client.post("/v1/audio/speech", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/v1/audio/speech", json=body, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    # host-level auth (e.g. a protected HF endpoint) can use Authorization; our token then rides in X-Orpheus-Token
    r = client.post("/v1/audio/speech", json=body, headers={"Authorization": "Bearer hf_x", "X-Orpheus-Token": "secret"})
    assert r.status_code == 200
    assert client.post("/v1/audio/speech", json={"input": "hi", "voice": "hannah"},
                       headers={"Authorization": "Bearer secret"}).status_code == 400
    assert model.calls[0] == ("<laugh> Hello", "tara")


def test_speech_while_loading_is_503(monkeypatch):
    client, _ = make(monkeypatch)
    assert client.post("/v1/audio/speech", json={"input": "hi"}, headers={"Authorization": "Bearer secret"}).status_code == 503


def test_stream_plan_emits_every_frame_once_in_order():
    for total in (1, 2, 3, 4, 7, 20):
        emitted, out = 0, []
        for frames_so_far in range(total + 1):              # frames arrive one at a time
            while (p := stream_plan(frames_so_far, emitted, False)) is not None:
                start, end, a, b = p
                assert start <= a < b <= end and end <= frames_so_far
                out += list(range(a, b)); emitted = b
        p = stream_plan(total, emitted, True)
        if p:
            out += list(range(p[2], p[3]))
        assert out == list(range(total)), total
        # while streaming, each emitted frame had 2 frames of right context
        assert stream_plan(5, 3, False) is None and stream_plan(6, 3, False) == (2, 6, 3, 4)


def test_stream_endpoint_returns_pcm(monkeypatch):
    client, model = make(monkeypatch)
    model.ready = True
    r = client.post("/v1/audio/speech/stream", json={"input": "hi", "voice": "leo"}, headers={"X-Orpheus-Token": "secret"})
    assert r.status_code == 200 and r.headers["x-sample-rate"] == "24000" and len(r.content) == 3 * 4096
    assert client.post("/v1/audio/speech/stream", json={"input": "hi"}).status_code == 401
