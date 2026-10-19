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
        self._owns_player = player is None
        self.player = player or Player(cfg.audio.output_device, OUT_SR)
        self.local_voice = local_voice
        self.vad = SileroVAD()
        from iriis.audio.turn import load_smart_turn
        self.turn = load_smart_turn(cfg.vad.smart_turn)
        self.sink: asyncio.Queue | None = None
        self.ws = None
        self.turn_done = asyncio.Event()
        self.turn_done.set()
        self.awaiting_confirm = False
        self.heard_reply = False          # has any of her reply played yet this turn?
        self.unfinished: np.ndarray | None = None   # your words so far, if you paused mid-thought
        self.last_utterance: np.ndarray | None = None
        self.confirm_question = ""
        self.confirm_played = False
        self.timings: dict[str, float] = {}
        self.followup_until = 0.0
        # GPU power: your voice wakes it, quiet puts it to sleep (iriis/desktop/gpu.py). A separate VAD
        # instance: Silero is stateful, and the recording endpointer owns self.vad.
        from iriis.desktop.gpu import GpuManager
        self.gpu = GpuManager(cfg.tts.gpu_endpoint, cfg.tts.gpu_idle_s)
        self.activity_vad = SileroVAD() if self.gpu.enabled else None
        self._speech_ms = 0.0

    # ------------------------------------------------------------------ mic plumbing (same pattern as the local loop)

    async def _pump(self):
        while True:
            block = await self.mic.read()
            if self.sink is not None:
                self.sink.put_nowait(block)
            if self.activity_vad is not None:
                try:
                    self._watch_activity(block)
                except Exception as e:  # noqa: BLE001  (power saving must never stop her listening)
                    self.ui.warn(f"activity detector error: {e}")

    def _watch_activity(self, block) -> None:
        """Any ~300 ms of your speech (wake word or not) wakes the GPU; her talking keeps it awake."""
        if self.player.busy or time.monotonic() - self.player.last_audio_end < 0.5:
            self.gpu.activity()          # she's talking: keep the GPU, but don't count her echo as you
            self.activity_vad.reset()
            self._speech_ms = 0.0
            return
        probs = self.activity_vad.process(block)
        frame_ms = 512 / self.cfg.audio.sample_rate * 1000
        for p in probs:
            self._speech_ms = self._speech_ms + frame_ms if p >= 0.6 else max(0.0, self._speech_ms - frame_ms)
        if self._speech_ms >= 300:
            self._speech_ms = 0.0
            if self.gpu.activity() and self.ws is not None:
                asyncio.get_running_loop().create_task(self._send_quietly({"type": "wake"}))

    async def _send_quietly(self, msg: dict) -> None:
        try:
            await self.ws.send(json.dumps(msg))
        except Exception:  # noqa: BLE001
            pass

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
        from iriis.desktop.agent import write_status
        window_start, loudest, best = time.monotonic(), 0.0, 0.0
        while True:
            block = await q.get()
            fired = self.wake.feed(block)
            loudest = max(loudest, float(np.sqrt(np.mean(block ** 2))))
            best = max(best, float(self.wake.last_score))
            if fired:
                self.ui.info(f"wake word heard (score {self.wake.last_score:.2f})")
                if self.ws is not None:
                    try:   # start waking her GPU voice while you're still asking your question
                        await self.ws.send(json.dumps({"type": "wake"}))
                    except Exception:  # noqa: BLE001
                        pass
                break
            if time.monotonic() - window_start >= 5:
                # Diagnostics: what she hears, visible in `iriis status` and the log.
                try:
                    write_status(mic_level=round(float(loudest), 4), wake_score=round(float(best), 3),
                                 wake_threshold=float(self.wake.threshold), mic_checked=time.time())
                except Exception:  # noqa: BLE001  (diagnostics must never stop her listening)
                    pass
                if loudest > 0.01:
                    self.ui.info(f"hearing sound: level {loudest:.3f}, best 'Hey Jarvis' score {best:.2f}"
                                 f" (fires at {self.wake.threshold:.2f})")
                window_start, loudest, best = time.monotonic(), 0.0, 0.0
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
        if preroll is None:
            # Her voice keeps bouncing around the room for a moment after she stops: don't record that.
            while self.player.busy or time.monotonic() - self.player.last_audio_end < 0.4:
                await asyncio.sleep(0.05)
            while not q.empty():
                q.get_nowait()
        for b in preroll or []:
            ep.feed(b)
        while True:
            ev = ep.feed(await q.get())
            if ev and ev.kind == "timeout":
                return None
            if ev and ev.kind == "speech_end":
                audio = ev.audio
                if audio is not None and self.turn is not None and not self.awaiting_confirm:
                    audio = await self._until_finished(q, audio)
                self.t_end = time.perf_counter()
                return audio

    async def _until_finished(self, q: asyncio.Queue, audio: np.ndarray) -> np.ndarray:
        """You paused. If your voice says you haven't finished your thought, keep listening a little
        longer and join what comes next, instead of answering half a sentence."""
        v = self.cfg.vad
        for _ in range(3):
            p = await asyncio.to_thread(self.turn.probability_complete, audio)
            if p >= v.smart_turn_threshold:
                return audio
            self.ui.info(f"(sounds unfinished, p={p:.2f}: waiting for the rest)")
            ep = Endpointer(self.vad, v.threshold, v.silence_ms, v.min_speech_ms, v.max_utterance_s, v.smart_turn_wait_s)
            more = None
            while True:
                ev = ep.feed(await q.get())
                if ev and ev.kind == "timeout":
                    return audio                     # you really were done
                if ev and ev.kind == "speech_end":
                    more = ev.audio
                    break
            if more is None:
                return audio
            gap = np.zeros(int(self.cfg.audio.sample_rate * 0.25), dtype=np.float32)
            audio = np.concatenate([audio, gap, more])
        return audio

    async def _barge_in(self) -> tuple[str, list[np.ndarray]]:
        """Returns ("continued", audio) if you keep talking before she has said anything (you only paused:
        your words get joined into one request), or ("barge", audio) if you talk over her while she speaks."""
        q = self._take_mic()
        vad = SileroVAD()
        need = max(1, int(self.cfg.barge_in.min_speech_ms / 32))
        need_continue = max(1, int(400 / 32))
        run, recent = 0, []
        while True:
            block = await q.get()
            recent = (recent + [block])[-12:]
            if not self.player.busy:
                if self.heard_reply:
                    run = 0          # a gap between her sentences: not you
                    continue
                # She hasn't started answering yet and you're talking again: you weren't finished.
                for p in vad.process(block):
                    run = run + 1 if p >= self.cfg.vad.threshold else max(0, run - 1)
                if run >= need_continue:
                    return "continued", recent
                continue
            rms = float(np.sqrt(np.mean(block ** 2)))
            echo = self.player.echo_level
            ratio = self.cfg.barge_in.echo_gate_ratio
            if getattr(self.player, "aec", None) is not None:
                ratio = min(ratio, 0.2)   # AEC leaves ~1-3% of her voice in the mic: a normal speaking voice gets through
            gated = echo > 0 and rms < echo * ratio
            for p in vad.process(block):
                run = run + 1 if (p >= self.cfg.vad.threshold and not gated) else max(0, run - 1)
            if run >= need:
                return "barge", recent

    # ------------------------------------------------------------------ server messages

    async def _receiver(self):
        pending_audio: dict | None = None
        async for raw in self.ws:
            if isinstance(raw, bytes):
                if pending_audio is not None:
                    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                    self.player.play(pcm, pending_audio.get("sample_rate", OUT_SR), tag=pending_audio.get("seq"))
                    self.heard_reply = True
                    if (self.awaiting_confirm and pending_audio.get("last", True)
                            and pending_audio.get("text", "").strip() == self.confirm_question.strip()):
                        self.confirm_played = True   # streamed: only once the question's final chunk arrived
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
                if ev.get("ignored"):
                    self.ui.info(f"(ignored my own voice: {ev.get('heard', '')[:60]})")
                    self.turn_done.set()
                    continue
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

    async def _wait_reply(self) -> tuple[str, list[np.ndarray]] | None:
        """Wait for the answer (or a confirmation question) to finish playing.
        Returns (kind, audio heard so far) if you start talking again; see _barge_in."""
        monitor = asyncio.create_task(self._barge_in()) if self.cfg.barge_in.enabled else None
        settled = asyncio.create_task(self._settled())
        finished, _ = await asyncio.wait({settled} | ({monitor} if monitor else set()), return_when=asyncio.FIRST_COMPLETED)
        if monitor and monitor in finished:
            settled.cancel()
            self.player.stop()
            if self.ws is not None:
                # Tell her exactly how far you'd heard, so she knows what you missed.
                await self.ws.send(json.dumps({"type": "interrupt", "heard_seq": self.player.current_tag}))
            kind, heard = monitor.result()
            self.ui.info("(you kept talking: waiting for the rest)" if kind == "continued" else "(interrupted)")
            return kind, heard
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
        self.mic = MicStream(cfg.audio.input_device, cfg.audio.sample_rate, cfg.audio.block_ms,
                             aec=getattr(self.player, "aec", None))
        await self.mic.start()
        self.gpu.start()
        pump = asyncio.create_task(self._pump())
        link = asyncio.create_task(self._keep_connected())
        trigger = {"wakeword": "Say 'Hey Jarvis'", "push_to_talk": f"Hold {cfg.wakeword.push_to_talk_key.upper()}", "always": "Just talk"}[cfg.wakeword.mode]
        self.ui.banner(f"{cfg.assistant_name} is listening. {trigger}.")
        try:
            await self._loop()   # keeps listening even while the brain is unreachable
        finally:
            link.cancel()
            pump.cancel()
            self.gpu.stop()
            await self.mic.stop()
            if self._owns_player:      # a shared speaker outlives a restart of the voice client
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
                # generous keepalive: Railway's proxy and a busy laptop can delay a pong past 20 s
                async with connect(url, additional_headers=headers, max_size=32 * 1024 * 1024,
                                   ping_interval=25, ping_timeout=60) as ws:
                    await ws.send(json.dumps({"type": "hello", "session_id": "owner", "speak": True}))
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
                wait = 8.0 if self.awaiting_confirm else self.cfg.wakeword.followup_s
                audio = await self._record(timeout_s=wait if followup else None)
            else:
                audio, preroll = await self._record(preroll=preroll), None
                if self.unfinished is not None:
                    # you paused and carried on: send everything as ONE request
                    first = self.unfinished
                    self.unfinished = None
                    gap = np.zeros(int(self.cfg.audio.sample_rate * 0.3), dtype=np.float32)
                    audio = np.concatenate([first, gap, audio]) if audio is not None else first
            self.followup_until = 0.0
            if audio is None:
                if self.awaiting_confirm and self.ws is not None:
                    await self.ws.send(json.dumps({"type": "confirm_reply", "approved": False}))
                elif self.cfg.wakeword.mode == "wakeword":
                    # Falling tone: "I've stopped listening; say Hey Jarvis to talk again."
                    if self.cfg.audio.chime:
                        self.player.play(chime(OUT_SR, rising=False), OUT_SR)
                    self.ui.info("(stopped listening: say 'Hey Jarvis' to talk again)")
                continue
            if self.ws is None:
                self.ui.warn("Not connected to my brain")
                if self.local_voice:
                    await self.local_voice.say(NOT_CONNECTED_REPLY)
                continue
            self.turn_done.clear()
            self.heard_reply = False
            self.last_utterance = audio
            self.ui.set_state("thinking")
            try:
                await self._send_utterance(audio)
            except Exception:  # noqa: BLE001  (link dropped mid-send)
                if self.local_voice:
                    await self.local_voice.say(NOT_CONNECTED_REPLY)
                continue
            waited = await self._wait_reply()
            preroll = None
            if waited is not None:
                kind, preroll = waited
                if kind == "continued":
                    self.unfinished = self.last_utterance
            if not self.awaiting_confirm:
                self.ui.latency({k: v for k, v in self.timings.items() if k != "first_audio"})
                if preroll is None:
                    self.followup_until = time.monotonic() + 1.0   # go straight into the follow-up window
