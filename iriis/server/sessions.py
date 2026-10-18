"""Conversation sessions and the turn runner shared by the SSE, WebSocket and MCP endpoints."""
from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from iriis.agent.loop import Agent, TurnCallbacks
from iriis.server.speech import pcm16, wav_bytes
from iriis.tts.text_normalize import Delivery, strip_directions

# emit(event_dict, binary_audio_or_None)
Emit = Callable[[dict[str, Any], bytes | None], Awaitable[None]]


@dataclass
class Session:
    id: str
    agent: Agent
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)   # confirmation id -> reply text
    last_used: float = field(default_factory=time.time)
    current: asyncio.Task | None = None

    def resolve_confirmation(self, confirm_id: str | None, reply: str) -> bool:
        if not self.pending:
            return False
        key = confirm_id if confirm_id in self.pending else next(iter(self.pending))
        fut = self.pending.pop(key)
        if not fut.done():
            fut.set_result(reply)
        return True


class SessionStore:
    def __init__(self, make_agent: Callable[[str], Agent], max_sessions: int = 200):
        self.make_agent = make_agent
        self.max_sessions = max_sessions
        self.sessions: OrderedDict[str, Session] = OrderedDict()

    def get(self, session_id: str | None) -> Session:
        sid = session_id or uuid.uuid4().hex[:12]
        s = self.sessions.get(sid)
        if s is None:
            s = Session(sid, self.make_agent(sid))   # reloads that conversation's saved context
            self.sessions[sid] = s
            while len(self.sessions) > self.max_sessions:
                old_id, old = next(iter(self.sessions.items()))
                if old.lock.locked():
                    break
                self.sessions.pop(old_id)
        self.sessions.move_to_end(sid)
        s.last_used = time.time()
        return s

    def drop(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None


async def run_turn(session: Session, text: str, emit: Emit, *, speech=None, speak: bool = False,
                   audio_format: str = "pcm", voice: str | None = None, confirm_timeout: float = 60.0,
                   allow_confirm: bool = True, timings: dict[str, float] | None = None) -> dict[str, Any]:
    """Run one agent turn, streaming events through `emit`.

    Events: sentence, audio (+binary or base64), tool, screen, confirm, done, error.
    audio_format: "pcm" = JSON header + raw PCM16 binary frame (WebSocket); "wav_b64" = base64 WAV inside the event (SSE).
    """
    timings = dict(timings or {})
    t0 = time.perf_counter()
    tts_q: asyncio.Queue[str | None] = asyncio.Queue()
    seq = 0

    delivery = Delivery(voice)   # the AI's [emotion] and {voice} choices carry across sentences

    async def tts_worker():
        nonlocal seq
        while (item := await tts_q.get()) is not None:
            sentence, is_question = item
            if is_question:
                spoken, use_voice = sentence, voice   # confirmations: always her own, plain voice
            else:
                spoken, use_voice = delivery.apply(sentence)
            try:
                audio, sr = await speech.synthesize(spoken, use_voice)
            except Exception as e:  # noqa: BLE001
                await emit({"type": "error", "message": f"TTS failed: {e}"}, None)
                continue
            if len(audio) == 0:
                continue
            if "first_audio_ms" not in timings:
                timings["first_audio_ms"] = (time.perf_counter() - t0) * 1000
            seq += 1
            if audio_format == "pcm":
                data = pcm16(audio)
                await emit({"type": "audio", "seq": seq, "sample_rate": sr, "format": "pcm_s16le", "bytes": len(data), "text": strip_directions(sentence)}, data)
            else:
                await emit({"type": "audio", "seq": seq, "format": "wav", "text": strip_directions(sentence),
                            "data": base64.b64encode(wav_bytes(audio, sr)).decode()}, None)

    worker = asyncio.create_task(tts_worker()) if speak and speech else None

    async def say(sentence: str) -> None:
        await emit({"type": "sentence", "text": strip_directions(sentence)}, None)
        if worker:
            await tts_q.put((sentence, False))

    async def confirm(question: str) -> str:
        if not allow_confirm:
            return ""
        cid = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        session.pending[cid] = fut
        await emit({"type": "confirm", "id": cid, "question": question, "session_id": session.id}, None)
        if worker:
            await tts_q.put((question, True))
        try:
            return await asyncio.wait_for(fut, confirm_timeout)
        except asyncio.TimeoutError:
            return ""
        finally:
            session.pending.pop(cid, None)

    def show(kind: str, text: str) -> None:
        asyncio.get_running_loop().create_task(emit({"type": kind, "text": text}, None))

    cb = TurnCallbacks(say=say, confirm=confirm, show=show)
    try:
        async with session.lock:
            result = await session.agent.handle(text, cb)
        if worker:
            await tts_q.put(None)
            await worker
        timings.update({k: v for k, v in result.timings.items()})
        done = {"type": "done", "session_id": session.id, "text": strip_directions(result.text),
                "tool_calls": [{"name": t["name"], "arguments": t["arguments"]} for t in result.tool_calls],
                "timings": {k: round(v) for k, v in timings.items()}}
        await emit(done, None)
        return done
    finally:
        if worker and not worker.done():
            worker.cancel()
