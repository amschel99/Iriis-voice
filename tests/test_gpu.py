"""GPU power manager: wake on speech, sleep after quiet, never on the event loop, never crash."""
from __future__ import annotations

import time

from iriis.desktop.gpu import GpuManager


class FakeEndpoint:
    def __init__(self, status):
        self.status = status


class FakeApi:
    def __init__(self):
        self.status = "running"
        self.calls = []

    def get_inference_endpoint(self, name):
        self.calls.append(("get", name))
        return FakeEndpoint(self.status)

    def scale_to_zero_inference_endpoint(self, name):
        self.calls.append(("scale_to_zero", name))
        self.status = "scaledToZero"


def make(monkeypatch, idle_s=0.2):
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    g = GpuManager("iriis-orpheus", idle_s=idle_s, check_s=0.05)
    api = FakeApi()
    g._api = lambda: api
    return g, api


def test_disabled_without_endpoint_or_token(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert not GpuManager("iriis-orpheus").enabled
    monkeypatch.setenv("HF_TOKEN", "x")
    assert not GpuManager("").enabled


def test_sleeps_after_quiet_and_only_once(monkeypatch):
    g, api = make(monkeypatch)
    g.start()
    time.sleep(0.6)
    g.stop()
    assert api.calls.count(("scale_to_zero", "iriis-orpheus")) == 1
    assert g.state == "scaledToZero"


def test_activity_keeps_it_awake(monkeypatch):
    g, api = make(monkeypatch, idle_s=0.3)
    g.start()
    t0 = time.time()
    while time.time() - t0 < 0.7:
        g.activity()
        time.sleep(0.05)
    g.stop()
    assert ("scale_to_zero", "iriis-orpheus") not in api.calls


def test_wake_requests_are_throttled(monkeypatch):
    g, _ = make(monkeypatch)
    assert g.activity() is True       # first speech: ask the brain to wake the GPU
    assert g.activity() is False      # talking on: don't spam
    g.last_wake_request -= 61
    assert g.activity() is True


def test_api_errors_never_kill_the_manager(monkeypatch):
    g, api = make(monkeypatch)

    def boom(name):
        raise RuntimeError("HF down")
    api.get_inference_endpoint = boom
    g.start()
    time.sleep(0.3)
    assert g._thread.is_alive()
    g.stop()


def test_does_not_scale_an_endpoint_that_is_already_off(monkeypatch):
    g, api = make(monkeypatch)
    api.status = "scaledToZero"
    g.start()
    time.sleep(0.4)
    g.stop()
    assert ("scale_to_zero", "iriis-orpheus") not in api.calls
    assert sum(1 for c in api.calls if c[0] == "get") == 1   # asked once, then remembers until new activity
