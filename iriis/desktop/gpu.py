"""Keep the paid voice GPU on only while you're actually talking.

* You start speaking (any speech: no wake word needed)  -> wake it, so it boots while you talk.
* She is speaking, or you spoke, within the last `idle_s` -> leave it alone.
* `idle_s` (default 5 min) of quiet from both of you     -> scale the Hugging Face endpoint to zero now
  (no GPU billing). Hugging Face wakes a scaled-to-zero endpoint automatically on the next request.

Threading rules (lessons from earlier bugs): every Hugging Face API call runs on this manager's own
worker thread, never on the asyncio event loop (a blocked loop misses WebSocket pings and the link
drops); the only cross-thread state is two floats and an Event, and failures are logged, never raised.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

API = "https://api.endpoints.huggingface.cloud/v2/endpoint"

log = logging.getLogger("iriis.gpu")


class GpuManager:
    def __init__(self, endpoint: str, idle_s: float = 300.0, token_env: str = "HF_TOKEN", check_s: float = 30.0):
        self.endpoint = endpoint
        self.idle_s = idle_s
        self.check_s = check_s
        self.token = os.environ.get(token_env, "")
        self.last_activity = time.monotonic()
        self.last_wake_request = 0.0
        self.state = "unknown"          # last status seen from Hugging Face
        self.slept_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.token)

    # ------------------------------------------------------------------ called from the voice client (cheap, thread-safe)
    def activity(self) -> bool:
        """You or she made a sound worth keeping the GPU for. Returns True when a wake-up should be
        requested now (throttled: at most once a minute, and only if we may have put it to sleep)."""
        now = time.monotonic()
        self.last_activity = now
        due = now - self.last_wake_request > 60
        if due:
            self.last_wake_request = now
        return due

    # ------------------------------------------------------------------ background worker
    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="gpu-idle", daemon=True)
        self._thread.start()
        log.info("GPU manager on: %s sleeps after %.0f s of quiet", self.endpoint, self.idle_s)

    def stop(self) -> None:
        self._stop.set()

    # Plain HTTPS to the Inference Endpoints API, NOT huggingface_hub: the desktop sets HF_HUB_OFFLINE=1 so
    # local models never touch the network, and the hub client honours that flag for these calls too.
    def _http(self, method: str, url: str) -> dict:
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        return json.loads(body) if body else {}

    def _path(self) -> str:
        if "/" not in self.endpoint:      # "name" -> "namespace/name" via whoami (once)
            me = self._http("GET", "https://huggingface.co/api/whoami-v2")
            self.endpoint = f"{me['name']}/{self.endpoint}"
        return f"{API}/{self.endpoint}"

    def status(self) -> str:
        try:
            self.state = self._http("GET", self._path()).get("status", {}).get("state", "unknown")
        except Exception as e:  # noqa: BLE001
            self.state = "unknown"
            log.warning("GPU status check failed: %s", e)
        return self.state

    def sleep_now(self) -> bool:
        try:
            self._http("POST", self._path() + "/scale-to-zero")
            self.slept_at = time.monotonic()
            self.state = "scaledToZero"
            log.info("GPU idle for %.0f s: scaled %s to zero", time.monotonic() - self.last_activity, self.endpoint)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("could not scale the GPU to zero: %s", e)
            return False

    def _run(self) -> None:
        while not self._stop.wait(self.check_s):
            try:
                idle = time.monotonic() - self.last_activity
                if idle < self.idle_s:
                    continue
                if self.slept_at is not None and self.slept_at > self.last_activity:
                    continue                          # already asleep since the last activity
                state = self.status()
                if state in ("running", "initializing", "updating", "pending"):
                    self.sleep_now()
                elif state == "unknown":
                    continue                          # couldn't ask: try again next check, never assume it's off
                else:
                    self.slept_at = time.monotonic()  # already off: remember, don't ask again until activity
            except Exception as e:  # noqa: BLE001  (the manager must never die)
                log.warning("GPU manager loop error: %s", e)
