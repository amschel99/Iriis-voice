"""Voice client for the cloud core: wake word + VAD + mic + speaker run here (cheap, offline);
speech recognition, the LLM and TTS run in the cloud over WS /v1/voice.

Barge-in: while Iriis is talking, local VAD (with an echo gate) listens; if you talk over it,
playback stops instantly, the server is told to `interrupt`, and your new words become the next request.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import numpy as np

from iriis.audio.capture import MicStream
from iriis.audio.playback import Player, chime
from iriis.audio.vad import Endpointer, SileroVAD
from iriis.config import Config
from iriis.ui.console import ConsoleUI

OUT_SR = 24000


NOT_CONNECTED_REPLY = "Sorry, I'm not connected to my brain right now, so I can't help with that yet. I'll tell you when I'm back."


class RemoteVoiceClient:
    def __init__(self, cfg: Config, ui: ConsoleUI, player: Player | None = None, local_voice=None):
        self.cfg, self.ui = cfg, ui
        self.player = player or Player(cfg.audio.output_device, OUT_SR)
        self.local_voice = local_voice
        self.vad = SileroVAD()
        self.sink: asyncio.Queue | None = None
        self.ws = None
        self.turn_done = asyncio.Event()
        self.turn_done.set()
        self.awaiting_confirm = False
        self.confirm_question = ""
        self.confirm_played = False
        self.timings: dict[str, float] = {}
        self.followup_until = 0.0

    # ------------------------------------------------------------------ mic plumbing (same pattern as the local loop)

    async def _pump(self):
        while True:
            block = await self.mic.read()
            if self.sink is not None:
                self.sink.put_nowait(block)

    def _take_mic(self) -> asyncio.Queue:
        self.sink = asyncio.Queue()
        return self.sink

    async def _trigger(self) -> None:
        mode = self.cfg.wakeword.mode
        if mode == "always" or time.monotonic() < self.followup_until or self.awaiting_confirm:
            return
        q = self._take_mic()
        self.ui.set_state("idle")
        if mode == "push_to_talk":
            while not self.ptt.pressed.is_set():
                await asyncio.sleep(0.02)
                while not q.empty():
                    q.get_nowait()
            return
        self.wake.reset()
        while not self.wake.feed(await q.get()):
            pass
        if self.cfg.audio.chime:
            self.player.play(chime(OUT_SR), OUT_SR)

    async def _record(self, timeout_s: float | None = None, preroll=None) -> np.ndarray | None:
        q = self._take_mic()
        self.ui.set_state("listening")
        if self.cfg.wakeword.mode == "push_to_talk" and self.ptt.pressed.is_set():
            chunks = list(preroll or [])
            while self.ptt.pressed.is_set():
                try:
                    chunks.append(await asyncio.wait_for(q.get(), 0.2))
                except asyncio.TimeoutError:
                    pass
            return np.concatenate(chunks) if chunks else None
        v = self.cfg.vad
        ep = Endpointer(self.vad, v.threshold, v.silence_ms, v.min_speech_ms, v.max_utterance_s, timeout_s or v.no_speech_timeout_s)
        for b in preroll or []:
            ep.feed(b)
        while True:
            ev = ep.feed(await q.get())
            if ev and ev.kind == "timeout":
                return None
            if ev and ev.kind == "speech_end":
                self.t_end = time.perf_counter()
                return ev.audio

    async def _barge_in(self) -> list[np.ndarray]:
        q = self._take_mic()
        vad = SileroVAD()
        need = max(1, int(self.cfg.barge_in.min_speech_ms / 32))
        run, recent = 0, []
        while True:
            block = await q.get()
            recent = (recent + [block])[-8:]
            rms = float(np.sqrt(np.mean(block ** 2)))
            gated = self.player.level > 0 and rms < self.player.level * self.cfg.barge_in.echo_gate_ratio
            for p in vad.process(block):
                run = run + 1 if (p >= self.cfg.vad.threshold and not gated) else max(0, run - 1)
            if run >= need:
                return recent

    # ------------------------------------------------------------------ server messages

    async def _receiver(self):
        pending_audio: dict | None = None
        async for raw in self.ws:
            if isinstance(raw, bytes):
                if pending_audio is not None:
                    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                    self.player.play(pcm, pending_audio.get("sample_rate", OUT_SR))
                    if self.awaiting_confirm and pending_audio.get("text") == self.confirm_question:
                        self.confirm_played = True
                    if "first_audio" not in self.timings and hasattr(self, "t_end"):
                        self.timings["end_of_speech_to_audio_ms"] = (time.perf_counter() - self.t_end) * 1000
                        self.timings["first_audio"] = 1
                        self.ui.set_state("speaking")
                    pending_audio = None
                continue
            ev = json.loads(raw)
            kind = ev.get("type")
            if kind == "audio":
                pending_audio = ev
            elif kind == "transcript":
                self.ui.user(ev.get("text") or "(didn't catch that)")
                if ev.get("stt_ms") is not None:
                    self.timings["stt_ms"] = ev["stt_ms"]
                if not ev.get("text") and not self.awaiting_confirm:
                    self.turn_done.set()
            elif kind == "sentence":
                self.ui.assistant_sentence(ev["text"])
            elif kind in ("tool", "screen"):
                self.ui.show(kind, ev.get("text", ""))
            elif kind == "confirm":
                self.awaiting_confirm = True
                self.confirm_question = ev.get("question", "")
                self.confirm_played = False
                self.ui.set_state("confirm")
                self.ui.assistant_sentence(self.confirm_question)
            elif kind in ("done", "interrupted", "error"):
                if kind == "error":
                    self.ui.error(ev.get("message", ""))
                if kind == "done":
                    self.timings.update({k: v for k, v in (ev.get("timings") or {}).items() if k != "first_audio"})
                self.awaiting_confirm = False
                self.turn_done.set()
            elif kind == "announce":
                self.ui.console.print(f"[bold yellow]⏰ {ev.get('text')}[/]")

    async def _send_utterance(self, audio: np.ndarray) -> None:
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        await self.ws.send(json.dumps({"type": "start_utterance"}))
        for i in range(0, len(pcm), 32000):
            await self.ws.send(pcm[i:i + 32000])
        await self.ws.send(json.dumps({"type": "end_utterance"}))

    async def _wait_reply(self) -> list[np.ndarray] | None:
        """Wait for the answer (or a confirmation question) to finish playing.
        Returns the audio heard so far if the user barges in."""
        monitor = asyncio.create_task(self._barge_in()) if self.cfg.barge_in.enabled else None
        settled = asyncio.create_task(self._settled())
        finished, _ = await asyncio.wait({settled} | ({monitor} if monitor else set()), return_when=asyncio.FIRST_COMPLETED)
        if monitor and monitor in finished:
            settled.cancel()
            self.player.stop()
            if self.ws is not None:
                await self.ws.send(json.dumps({"type": "interrupt"}))
            self.ui.info("(interrupted)")
            return monitor.result()
        if monitor:
            monitor.cancel()
        return None

    async def _settled(self) -> None:
        asked_at = None
        while True:
            if self.awaiting_confirm and asked_at is None:
                asked_at = time.monotonic()
            quiet = not self.player.busy and self.player.q.empty()
            if quiet and self.turn_done.is_set():
                return
            if quiet and self.awaiting_confirm and (self.confirm_played or time.monotonic() - asked_at > 6):
                return  # the question has been heard (or TTS is off); the next utterance answers it
            await asyncio.sleep(0.03)

    # ------------------------------------------------------------------ main

    async def run(self) -> None:
        cfg = self.cfg
        if cfg.wakeword.mode == "wakeword":
            from iriis.audio.wakeword import WakeWord
            self.wake = WakeWord(cfg.wakeword)
        elif cfg.wakeword.mode == "push_to_talk":
            from iriis.audio.wakeword import PushToTalk
            self.ptt = PushToTalk(cfg.wakeword.push_to_talk_key)
        self.mic = MicStream(cfg.audio.input_device, cfg.audio.sample_rate, cfg.audio.block_ms)
        await self.mic.start()
        pump = asyncio.create_task(self._pump())
        link = asyncio.create_task(self._keep_connected())
        trigger = {"wakeword": "Say 'Hey Jarvis'", "push_to_talk": f"Hold {cfg.wakeword.push_to_talk_key.upper()}", "always": "Just talk"}[cfg.wakeword.mode]
        self.ui.banner(f"{cfg.assistant_name} is listening. {trigger}.")
        try:
            await self._loop()   # keeps listening even while the brain is unreachable
        finally:
            link.cancel()
            pump.cancel()
            await self.mic.stop()
            self.player.close()

    async def _keep_connected(self) -> None:
        """Hold the voice WebSocket open, reconnecting forever. self.ws is None while down."""
        from websockets.asyncio.client import connect
        from iriis.desktop.agent import write_status
        from iriis.envfile import to_ws

        key = os.environ.get(self.cfg.desktop.api_key_env, "")
        url = to_ws(self.cfg.desktop.server_url) + "/v1/voice"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        while True:
            try:
                async with connect(url, additional_headers=headers, max_size=32 * 1024 * 1024, ping_interval=20) as ws:
                    await ws.send(json.dumps({"type": "hello", "session_id": "desktop-voice", "speak": True}))
                    ready = json.loads(await ws.recv())
                    self.ws = ws
                    write_status(voice_connected=True, stt=ready.get("stt"), tts=ready.get("tts"))
                    self.ui.info(f"Voice connected (STT {ready.get('stt')}, TTS {ready.get('tts')})")
                    await self._receiver()
            except Exception as e:  # noqa: BLE001
                write_status(voice_connected=False, voice_error=f"{e.__class__.__name__}: {e}"[:200])
            finally:
                if self.ws is not None:
                    self.ui.warn("Voice link down; reconnecting")
                self.ws = None
                self.awaiting_confirm = False
                self.turn_done.set()
            await asyncio.sleep(3)

    async def _loop(self) -> None:
        preroll = None
        while True:
            self.timings = {}
            if preroll is None:
                await self._trigger()
                followup = time.monotonic() < self.followup_until or self.awaiting_confirm
                audio = await self._record(timeout_s=(8.0 if self.awaiting_confirm else 4.0) if followup else None)
            else:
                audio, preroll = await self._record(preroll=preroll), None
            self.followup_until = 0.0
            if audio is None:
                if self.awaiting_confirm and self.ws is not None:
                    await self.ws.send(json.dumps({"type": "confirm_reply", "approved": False}))
                continue
            if self.ws is None:
                self.ui.warn("Not connected to my brain")
                if self.local_voice:
                    await self.local_voice.say(NOT_CONNECTED_REPLY)
                continue
            self.turn_done.clear()
            self.ui.set_state("thinking")
            try:
                await self._send_utterance(audio)
            except Exception:  # noqa: BLE001  (link dropped mid-send)
                if self.local_voice:
                    await self.local_voice.say(NOT_CONNECTED_REPLY)
                continue
            preroll = await self._wait_reply()
            if not self.awaiting_confirm:
                self.ui.latency({k: v for k, v in self.timings.items() if k != "first_audio"})
                if preroll is None:
                    self.followup_until = time.monotonic() + 5.0
