"""Full voice loop: wake word / push-to-talk -> VAD endpointing -> STT -> agent -> streaming TTS,
with barge-in, spoken confirmation, reminders and per-stage latency.

Audio threads (PortAudio capture, the player) never wait on the LLM: the mic pump hands
blocks to whichever consumer currently owns the mic, and the agent runs as its own task.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Callable

import numpy as np

from iriis.agent.loop import TurnCallbacks
from iriis.audio.capture import MicStream
from iriis.audio.playback import Player, chime
from iriis.audio.vad import Endpointer, SileroVAD
from iriis.config import Config
from iriis.stt import make_stt
from iriis.tts import make_tts
from iriis.tts.text_normalize import normalize
from iriis.ui.console import ConsoleUI


class Speaker:
    """Sentence queue -> text normalisation -> synthesis (worker thread) -> Player."""

    def __init__(self, cfg: Config, ui: ConsoleUI):
        self.cfg, self.ui = cfg, ui
        try:
            self.tts = make_tts(cfg.tts)
        except Exception as e:  # noqa: BLE001
            if cfg.tts.backend == "piper":
                raise
            ui.warn(f"Kokoro unavailable ({e}); falling back to Piper.")
            self.tts = make_tts(cfg.tts, "piper")
        self.player = Player(cfg.audio.output_device, self.tts.sample_rate)
        self.queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self.generation = 0
        self._pending = 0
        self._done = asyncio.Event()
        self._done.set()
        self.on_first_audio: Callable[[], None] | None = None
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self.tts.warmup)
        self._worker = asyncio.create_task(self._run(), name="tts")

    async def say(self, sentence: str) -> None:
        self._pending += 1
        self._done.clear()
        await self.queue.put((self.generation, sentence))

    def play_chime(self, rising: bool = True) -> None:
        if self.cfg.audio.chime:
            self.player.play(chime(self.player.sample_rate, rising), self.player.sample_rate)

    async def _run(self) -> None:
        while True:
            gen, sentence = await self.queue.get()
            try:
                if gen != self.generation:
                    continue
                text = normalize(sentence, self.cfg.tts.currency)
                if not text or not re.search(r"\w", text):
                    continue
                audio = await asyncio.to_thread(self.tts.synthesize, text)
                if gen != self.generation:
                    continue
                self.player.play(audio, self.tts.sample_rate)
                if self.on_first_audio:
                    self.on_first_audio()
                    self.on_first_audio = None
            except Exception as e:  # noqa: BLE001
                self.ui.error(f"TTS failed: {e}")
            finally:
                self._pending -= 1
                if self._pending <= 0:
                    self._pending = 0
                    self._done.set()

    @property
    def speaking(self) -> bool:
        return self._pending > 0 or self.player.busy

    def interrupt(self) -> None:
        """Drop queued sentences and cut current playback immediately."""
        self.generation += 1
        self.player.stop()

    async def wait_done(self) -> None:
        await self._done.wait()
        while self.player.busy:
            await asyncio.sleep(0.03)

    def close(self) -> None:
        if self._worker:
            self._worker.cancel()
        self.player.close()


class VoiceLoop:
    def __init__(self, app):
        self.app = app
        self.cfg: Config = app.cfg
        self.ui: ConsoleUI = app.ui
        self.sink: asyncio.Queue[np.ndarray] | None = None   # who currently owns the mic
        self.followup_until = 0.0

    # ------------------------------------------------------------------ mic plumbing

    async def _pump(self) -> None:
        while True:
            block = await self.mic.read()
            if self.sink is not None:
                self.sink.put_nowait(block)

    def _take_mic(self) -> asyncio.Queue[np.ndarray]:
        q: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.sink = q
        return q

    # ------------------------------------------------------------------ stages

    async def wait_for_trigger(self) -> None:
        mode = self.cfg.wakeword.mode
        if mode == "always" or time.monotonic() < self.followup_until:
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
        while True:
            block = await q.get()
            if self.wake.feed(block):  # ~1 ms per 80 ms block; fine inline
                self.speaker.play_chime(True)
                return

    async def record_utterance(self, timeout_s: float | None = None, preroll: list[np.ndarray] | None = None) -> np.ndarray | None:
        """Record until the user stops talking (VAD), or until the push-to-talk key is released."""
        q = self._take_mic()
        self.ui.set_state("listening")
        if self.cfg.wakeword.mode == "push_to_talk" and self.ptt.pressed.is_set():
            chunks = list(preroll or [])
            while self.ptt.pressed.is_set():
                try:
                    chunks.append(await asyncio.wait_for(q.get(), 0.2))
                except asyncio.TimeoutError:
                    pass
            while not q.empty():
                chunks.append(q.get_nowait())
            return np.concatenate(chunks) if chunks else None
        v = self.cfg.vad
        ep = Endpointer(self.vad, v.threshold, v.silence_ms, v.min_speech_ms, v.max_utterance_s,
                        timeout_s or v.no_speech_timeout_s)
        for block in preroll or []:
            ev = ep.feed(block)
        while True:
            block = await q.get()
            ev = ep.feed(block)
            if ev is None or ev.kind == "speech_start":
                continue
            if ev.kind == "timeout":
                return None
            self.t_end_of_speech = time.perf_counter()
            return ev.audio

    async def transcribe(self, audio: np.ndarray) -> str:
        self.ui.set_state("thinking")
        tr = await asyncio.to_thread(self.stt.transcribe, audio)
        self.timings["stt_ms"] = tr.elapsed_s * 1000
        return tr.text

    async def listen_for_reply(self) -> str:
        """Used for confirmations: speak first, then capture one short reply."""
        await self.speaker.wait_done()
        audio = await self.record_utterance(timeout_s=8.0)
        if audio is None:
            return ""
        text = await self.transcribe(audio)
        self.ui.user(text or "(nothing)")
        return text

    async def barge_in_monitor(self) -> list[np.ndarray]:
        """Returns (with the audio heard so far) when the user talks over Iriis."""
        q = self._take_mic()
        vad = SileroVAD()
        needed = max(1, int(self.cfg.barge_in.min_speech_ms / 32))
        run, recent = 0, []
        while True:
            block = await q.get()
            recent = (recent + [block])[-8:]
            mic_rms = float(np.sqrt(np.mean(block ** 2)))
            playing = self.speaker.player.level
            # Echo gate: while Iriis is audible, the mic must be clearly louder than the playback.
            gated = playing > 0 and mic_rms < playing * self.cfg.barge_in.echo_gate_ratio
            for p in vad.process(block):
                if p >= self.cfg.vad.threshold and not gated:
                    run += 1
                else:
                    run = max(0, run - 1)
            if run >= needed:
                return recent

    # ------------------------------------------------------------------ one turn

    def _is_stop_word(self, text: str) -> bool:
        t = re.sub(r"[^a-z ]", "", text.lower()).strip()
        return t in {w.lower() for w in self.cfg.barge_in.stop_words} or t in {f"{self.cfg.assistant_name.lower()} {w}" for w in ("stop", "cancel")}

    async def run_turn(self, text: str) -> list[np.ndarray] | None:
        """Run the agent; returns preroll audio if the user barged in."""
        agent = self.app.agent
        t_turn = time.perf_counter()

        def first_audio():
            self.timings["first_audio_ms"] = (time.perf_counter() - t_turn) * 1000
            if hasattr(self, "t_end_of_speech"):
                self.timings["end_of_speech_to_audio_ms"] = (time.perf_counter() - self.t_end_of_speech) * 1000
            self.ui.set_state("speaking")

        self.speaker.on_first_audio = first_audio

        async def say(sentence: str) -> None:
            self.ui.assistant_sentence(sentence)
            await self.speaker.say(sentence)

        confirming = asyncio.Event()

        async def confirm(question: str) -> str:
            confirming.set()
            try:
                self.ui.set_state("confirm")
                await say(question)
                return await self.listen_for_reply()
            finally:
                confirming.clear()
                self.ui.set_state("thinking")

        cb = TurnCallbacks(say=say, confirm=confirm, show=self.ui.show)
        task = asyncio.create_task(agent.handle(text, cb), name="agent-turn")
        barge: list[np.ndarray] | None = None
        while True:
            if confirming.is_set():
                await asyncio.sleep(0.05)  # confirmation owns the mic
                if task.done() and not self.speaker.speaking:
                    break
                continue
            monitor = asyncio.create_task(self.barge_in_monitor()) if self.cfg.barge_in.enabled else None
            done_waiter = asyncio.create_task(self._wait_turn_done(task, confirming))
            waiters = {done_waiter} | ({monitor} if monitor else set())
            finished, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if monitor and monitor in finished:
                barge = monitor.result()
                done_waiter.cancel()
                task.cancel()
                self.speaker.interrupt()
                self.ui.info("(interrupted)")
                break
            if monitor:
                monitor.cancel()
            if done_waiter.result() == "confirm":
                continue
            break
        if not task.cancelled() and task.done() and task.exception():
            self.ui.error(f"{task.exception()}")
        elif task.done() and not task.cancelled():
            self.timings.update({k: v for k, v in task.result().timings.items() if k == "first_token_ms"})
        return barge

    async def _wait_turn_done(self, task: asyncio.Task, confirming: asyncio.Event) -> str:
        while True:
            if confirming.is_set():
                return "confirm"
            if task.done() and not self.speaker.speaking:
                return "done"
            await asyncio.sleep(0.03)

    # ------------------------------------------------------------------ main loop

    async def run(self) -> None:
        cfg, ui = self.cfg, self.ui
        problems = await self.app.start()
        ui.info("Loading speech models...")
        self.vad = SileroVAD()
        self.stt = await asyncio.to_thread(make_stt, cfg.stt)
        await asyncio.to_thread(self.stt.warmup)
        ui.info(f"STT: {self.stt.name}")
        self.speaker = Speaker(cfg, ui)
        await self.speaker.start()
        ui.info(f"TTS: {self.speaker.tts.name}")
        if cfg.wakeword.mode == "wakeword":
            from iriis.audio.wakeword import WakeWord
            self.wake = WakeWord(cfg.wakeword)
        elif cfg.wakeword.mode == "push_to_talk":
            from iriis.audio.wakeword import PushToTalk
            self.ptt = PushToTalk(cfg.wakeword.push_to_talk_key)
        self.mic = MicStream(cfg.audio.input_device, cfg.audio.sample_rate, cfg.audio.block_ms)
        await self.mic.start()
        pump = asyncio.create_task(self._pump(), name="mic-pump")

        async def announce(text: str) -> None:
            ui.console.print(f"[bold yellow]⏰ {text}[/]")
            await self.speaker.say(text)

        self.app.start_reminders(announce)
        trigger = {"wakeword": "Say 'Hey Jarvis'", "push_to_talk": f"Hold {cfg.wakeword.push_to_talk_key.upper()} to talk", "always": "Just talk"}[cfg.wakeword.mode]
        ui.banner(f"{cfg.assistant_name} is ready. {trigger}. Ctrl+C to quit.")
        for p in problems:
            ui.warn(p)
            await self.speaker.say(p)

        preroll: list[np.ndarray] | None = None
        try:
            while True:
                self.timings: dict[str, float] = {}
                if preroll is None:
                    await self.wait_for_trigger()
                    in_followup = time.monotonic() < self.followup_until
                    audio = await self.record_utterance(timeout_s=4.0 if in_followup else None)
                else:
                    audio = await self.record_utterance(preroll=preroll)
                    preroll = None
                self.followup_until = 0.0
                if audio is None:
                    continue
                text = await self.transcribe(audio)
                if not text:
                    ui.info("(didn't catch that)")
                    continue
                ui.user(text)
                if self._is_stop_word(text):
                    self.speaker.interrupt()
                    continue
                preroll = await self.run_turn(text)
                ui.latency(self.timings)
                if preroll is None:
                    self.followup_until = time.monotonic() + 5.0  # short follow-up window, no wake word needed
        finally:
            pump.cancel()
            await self.mic.stop()
            self.speaker.close()
            await self.app.stop()


async def run_voice(app) -> None:
    await VoiceLoop(app).run()
