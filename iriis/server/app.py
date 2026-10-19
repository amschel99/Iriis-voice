"""Iriis cloud core: HTTP/SSE + WebSocket voice + MCP + llms.txt, and the desktop link.

    uvicorn iriis.server.app:app --host 0.0.0.0 --port $PORT
    python -m iriis.server
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from mcp.server.mcpserver.utilities.types import Audio  # module level: MCP evaluates the annotation
from pydantic import BaseModel, Field

from iriis.agent.loop import Agent
from iriis.agent.conversations import ConversationStore
from iriis.agent.longterm import LongTermMemory
from iriis.agent.permissions import AuditLog, PermissionManager
from iriis.agent.reminders import ReminderWatcher
from iriis.config import Config, apply_offline_env, load_config, resolve_path
from iriis.mcp_client.manager import MCPManager
from iriis.server.desktop_bridge import CompositeTools, DesktopBridge
from iriis.server.sessions import Session, SessionStore, run_turn
from iriis.server.speech import Speech, decode_audio_file, from_pcm16, wav_bytes
from iriis.storage import get_db

log = logging.getLogger("iriis.server")
_BACKGROUND: set = set()     # chat turns still running after their app closed
STATIC = Path(__file__).parent / "static"


class Core:
    """Everything shared by all requests."""

    def __init__(self, cfg: Config):
        from iriis.app import make_llm

        self.cfg = cfg
        self.llm = make_llm(cfg)
        self.mcp = MCPManager(cfg, only=cfg.server.cloud_servers)
        self.permissions = PermissionManager(cfg, [])
        self.desktop = DesktopBridge(self.permissions)
        self.tools = CompositeTools(self.mcp, self.desktop)
        self.db = get_db()
        self.memory = LongTermMemory(self.db) if cfg.memory.enabled else None
        self.conversations = ConversationStore(self.db, write_behind=True)
        self.speech = Speech(cfg)
        # Audit trail: JSONL file + the database (so it survives redeploys on Railway).
        self.audit = AuditLog(cfg.permissions.audit_log, db=self.db, source="cloud")
        self.sessions = SessionStore(self._make_agent, cfg.server.max_sessions)
        self.voice_sockets: set[WebSocket] = set()
        from iriis.server.telegram import TelegramBridge
        self.telegram = TelegramBridge(self)
        self.problems: list[str] = []
        self.started = time.time()
        from iriis.agent.mind import Mind
        self.mind = Mind(self)          # her memory stream: what she learns about the user, lessons, goals, mood

    def _make_agent(self, session_id: str, cfg: Config | None = None) -> Agent:
        facts = (lambda: [t for _, t in self.memory.all()]) if self.memory else None
        agent = Agent(cfg or self.cfg, self.llm, self.tools, self.permissions, facts,
                      store=self.conversations, session_id=session_id)
        agent.audit = self.audit
        if not session_id.startswith("mind"):
            agent.mind = self.mind
        return agent

    async def run_task(self, task_id: int, prompt: str) -> str:
        """One background task as an agent turn: its own session, more tool steps, no confirmations."""
        from iriis.server.sessions import Session, run_turn
        cfg = self.cfg.model_copy(deep=True)
        cfg.llm.max_tool_iterations = max(cfg.llm.max_tool_iterations, 20)
        cfg.llm.max_tokens = max(cfg.llm.max_tokens, 4096)
        cfg.llm.history_max_tokens = max(cfg.llm.history_max_tokens, 30000)   # room for the pages it reads
        sid = f"task-{task_id}"
        session = Session(sid, self._make_agent(sid, cfg))

        async def ignore(ev, _audio):
            return None

        done = await run_turn(session, prompt, ignore, allow_confirm=False)
        return done["text"]

    async def notify(self, spoken: str, full: str = "") -> None:
        """Task reports: a short line out loud, the whole report in Telegram, a notification on every device."""
        await self.announce(spoken, telegram=False, push=False)
        await self.telegram.broadcast(full or spoken)
        await self.push(spoken)

    async def push(self, text: str) -> None:
        from iriis.server.webapp import push_all
        try:
            await push_all(self.db, "Iriis", text)
        except Exception as e:  # noqa: BLE001  (notifications are best effort)
            log.warning("push failed: %s", e)

    async def start(self) -> None:
        ok, msg = await self.llm.health()
        log.info(msg)
        if not ok:
            self.problems.append(msg)
        self.problems += await self.mcp.start()
        self.permissions.add_servers(self.mcp.server_configs)
        self.problems += await self.speech.preload()
        if self.db.dialect == "sqlite" and os.environ.get("RAILWAY_ENVIRONMENT"):
            self.problems.append("No DATABASE_URL: memories, notes and reminders are in SQLite and will be lost on redeploy. "
                                 "Add Railway Postgres and set DATABASE_URL=${{Postgres.DATABASE_URL}}.")
        log.info("storage: %s", self.db.description)
        for p in self.problems:
            log.warning(p)

    async def announce(self, text: str, telegram: bool = True, push: bool = True) -> None:
        """Reminders and timers go to the desktop, every open voice socket, Telegram and app notifications."""
        if telegram:
            await self.telegram.broadcast(text)
        if push:
            await self.push(text)
        await self.desktop.send({"type": "announce", "text": text})
        for ws in list(self.voice_sockets):
            try:
                await ws.send_json({"type": "announce", "text": text})
            except Exception:  # noqa: BLE001
                self.voice_sockets.discard(ws)

    async def stop(self) -> None:
        n = await asyncio.to_thread(self.conversations.flush)   # redeploy/shutdown: nothing is lost
        log.info("flushed %d conversation messages on shutdown", n)
        await self.mcp.stop()
        await self.telegram.aclose()
        await self.llm.aclose()


# ---------------------------------------------------------------------------- auth

def _secret(env: str) -> str:
    return os.environ.get(env, "")


def _check(token: str, secret: str, client_host: str | None) -> bool:
    if not secret:
        # No key configured: only allow callers on this machine (local development).
        return client_host in ("127.0.0.1", "::1", "localhost", "testclient")
    return hmac.compare_digest(token.encode(), secret.encode())


def _token_from(headers, query) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    tok = query.get("token", "") or headers.get("x-api-key", "")
    if not tok and headers.get("cookie"):          # the Iriis app logs in once and keeps an HttpOnly cookie
        from http.cookies import SimpleCookie
        try:
            c = SimpleCookie(headers.get("cookie"))
            tok = c["iriis_key"].value if "iriis_key" in c else ""
        except Exception:  # noqa: BLE001
            tok = ""
    return tok


async def require_key(request: Request) -> None:
    core: Core = request.app.state.core
    if not _check(_token_from(request.headers, request.query_params), _secret(core.cfg.server.api_key_env),
                  request.client.host if request.client else None):
        raise HTTPException(401, "missing or wrong API key (Authorization: Bearer <IRIIS_API_KEY>)")


async def ws_authorised(ws: WebSocket, env: str) -> bool:
    if _check(_token_from(ws.headers, ws.query_params), _secret(env), ws.client.host if ws.client else None):
        return True
    await ws.close(code=4401, reason="unauthorised")
    return False


# ---------------------------------------------------------------------------- MCP (Iriis as a server)

def build_mcp(core_ref: dict):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("iriis", instructions=(
        "Iriis is a personal assistant with tools on its owner's computer (files, apps, shell, system info), "
        "notes, reminders and long-term memory. Use ask_iriis for anything you'd ask a human assistant. "
        "Actions that need the owner's confirmation are declined over MCP; the owner must confirm them by voice or in chat."))

    @mcp.tool()
    async def ask_iriis(message: str, session_id: str = "mcp") -> str:
        """Send a request to Iriis and get its text reply. Reuse session_id to continue a conversation."""
        core: Core = core_ref["core"]
        out: list[str] = []

        async def emit(ev, _audio):
            if ev["type"] == "screen":
                out.append(f"\n[on screen] {ev['text']}")

        done = await run_turn(core.sessions.get(session_id), message, emit, allow_confirm=False)
        return done["text"] + "".join(out)

    @mcp.tool()
    async def speak(text: str, voice: str = "") -> Audio:
        """Turn text into speech with Iriis' voice. Returns WAV audio."""
        core: Core = core_ref["core"]
        audio, sr = await core.speech.synthesize(text, voice or None)
        return Audio(data=wav_bytes(audio, sr), format="wav")

    @mcp.tool()
    async def iriis_status() -> str:
        """Whether Iriis' tools, desktop link and speech models are up."""
        core: Core = core_ref["core"]
        return json.dumps(status_dict(core))

    return mcp


def status_dict(core: Core) -> dict[str, Any]:
    return {
        "ok": True,
        "uptime_s": round(time.time() - core.started),
        "llm": {"backend": core.cfg.llm.backend, "model": core.llm.model},
        "stt": core.speech.stt_name,
        "tts": core.speech.tts_name,
        "storage": core.db.description,
        "unsaved_messages": core.conversations.pending,
        "desktop_connected": core.desktop.online,
        "telegram": {"enabled": core.telegram.enabled, "allowed_chats": len(core.telegram.allowed)},
        "cloud_servers": core.mcp.status(),
        "tools": sum(len(s["tools"]) for s in core.tools.servers_catalog().values()),
        "problems": core.problems,
    }


class _MCPAuth:
    """ASGI wrapper: the MCP endpoint needs the same API key as the REST API."""

    def __init__(self, app, core_ref):
        self.app, self.core_ref = app, core_ref

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            core: Core = self.core_ref["core"]
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            from urllib.parse import parse_qs
            query = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode()).items()}
            client = (scope.get("client") or [None])[0]
            if not _check(_token_from(headers, query), _secret(core.cfg.server.api_key_env), client):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"unauthorised"}'})
                return
        await self.app(scope, receive, send)



# ---------------------------------------------------------------------------- request bodies

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    stream: bool = True
    speak: bool = Field(False, description="also return spoken audio (base64 WAV per sentence)")
    voice: str | None = None
    background: bool = Field(False, description="keep working if the client disconnects (the reply is saved to the "
                                                "conversation and a notification is sent when it's done)")


class ConfirmRequest(BaseModel):
    session_id: str
    id: str | None = None
    approved: bool | None = None
    text: str | None = None


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None


# ---------------------------------------------------------------------------- app

def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    core_ref: dict[str, Core] = {}
    mcp = build_mcp(core_ref)
    from mcp.server.transport_security import TransportSecuritySettings
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # API key auth instead
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        core = Core(cfg)
        core_ref["core"] = core
        app.state.core = core
        await core.start()
        reminders = asyncio.create_task(ReminderWatcher(core.announce, core.db).run())
        flusher = asyncio.create_task(core.conversations.run_flusher(cfg.memory.flush_interval_s))
        from iriis.agent.tasks import TaskRunner
        from iriis.agent.mind import run_calls
        background = [asyncio.create_task(TaskRunner(core.run_task, core.notify, core.db).run()),
                      asyncio.create_task(core.telegram.run()),
                      asyncio.create_task(run_calls(core))]          # rings the alarms and calls the user set
        if not os.environ.get("IRIIS_NO_MIND"):
            # her own thinking between conversations; she only ever reaches the user (the owner approved this)
            background.append(asyncio.create_task(core.mind.run()))
        async with mcp.session_manager.run():
            yield
        reminders.cancel()
        flusher.cancel()
        for t in background:
            t.cancel()
        await core.stop()

    app = FastAPI(title="Iriis", version="0.2.0", lifespan=lifespan,
                  description="Personal voice assistant API. See /llms.txt for a compact guide.")

    # ------------------------------------------------------------------ public

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms_txt(request: Request):
        base = str(request.base_url).rstrip("/")
        return (STATIC / "llms.txt").read_text(encoding="utf-8").replace("{BASE_URL}", base)

    @app.get("/", include_in_schema=False)
    async def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/app", status_code=307)       # the Iriis app (API guide: /llms.txt, OpenAPI: /docs)

    from iriis.server import camera, webapp
    app.include_router(webapp.setup(require_key))
    app.include_router(camera.setup(require_key, ws_authorised, cfg.server.api_key_env, core_ref))

    # ------------------------------------------------------------------ REST


    @app.get("/v1/status", dependencies=[Depends(require_key)])
    async def status(request: Request):
        return status_dict(request.app.state.core)

    @app.post("/v1/chat", dependencies=[Depends(require_key)])
    async def chat(req: ChatRequest, request: Request):
        core: Core = request.app.state.core
        session = core.sessions.get(req.session_id)
        if req.speak:
            core.speech.warm_up()
        if not req.stream:
            async def ignore(ev, audio):
                pass
            done = await run_turn(session, req.message, ignore, speech=core.speech, speak=False,
                                  confirm_timeout=core.cfg.server.confirm_timeout_s)
            return done

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        gone = {"yes": False}           # the app was closed while she was still working

        async def emit(ev, _audio):
            if gone["yes"]:
                if ev.get("type") == "confirm":         # she needs a yes and nobody is looking: ask on the phone
                    await core.push(f"Iriis needs your OK: {ev['question']}")
                return
            await queue.put(ev)

        async def runner():
            try:
                done = await run_turn(session, req.message, emit, speech=core.speech, speak=req.speak and not gone["yes"],
                                      audio_format="wav_b64", voice=req.voice,
                                      confirm_timeout=600 if req.background else core.cfg.server.confirm_timeout_s)
                if gone["yes"] and (done.get("text") or "").strip():
                    await core.push(done["text"].strip()[:220])          # finished while the app was closed
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("chat turn failed")
                await queue.put({"type": "error", "message": str(e)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

        async def sse():
            yield f"event: session\ndata: {json.dumps({'session_id': session.id})}\n\n"
            try:
                while (ev := await queue.get()) is not None:
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
            finally:
                if not task.done():
                    if req.background:
                        gone["yes"] = True      # app closed: she carries on, saves the reply and notifies
                    else:
                        task.cancel()           # client went away = interrupt

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


    @app.get("/v1/sessions/{session_id}/state", dependencies=[Depends(require_key)])
    async def session_state(session_id: str, request: Request):
        """Is she still working on this conversation, and is she waiting for a yes? (for an app that reopens)"""
        core: Core = request.app.state.core
        s = core.sessions.sessions.get(session_id)
        if not s:
            return {"busy": False, "pending": []}
        return {"busy": s.lock.locked(),
                "pending": [{"id": i, "question": q, "session_id": session_id} for i, q in s.questions.items()]}

    @app.get("/v1/settings/autonomy", dependencies=[Depends(require_key)])
    async def autonomy_get(request: Request):
        """Whether Iriis thinks and acts on her own between conversations (messages and calls to the user only)."""
        core: Core = request.app.state.core
        return {"enabled": await asyncio.to_thread(lambda: core.mind.autonomous)}

    @app.post("/v1/settings/autonomy", dependencies=[Depends(require_key)])
    async def autonomy_set(request: Request):
        core: Core = request.app.state.core
        on = bool((await request.json()).get("enabled"))
        await asyncio.to_thread(core.mind.set_autonomous, on)
        return {"enabled": on}

    @app.get("/v1/mind", dependencies=[Depends(require_key)])
    async def mind_view(request: Request, kind: str = ""):
        """What she has learned: goals, mood, insights, the user's patterns, people, lessons, skills, open loops."""
        core: Core = request.app.state.core
        from iriis.agent.mind import KINDS
        kinds = (kind,) if kind in KINDS else KINDS

        def load():
            rows = core.mind.rows(kinds, 400)
            return {"mood": core.mind.mood(), "persona": core.mind.persona(), "plan": core.mind.agenda.plan(),
                    "items": [{"id": r[0], "kind": r[1], "text": r[2], "importance": r[3], "created": r[4]} for r in rows]}
        return await asyncio.to_thread(load)

    @app.get("/v1/training/export", dependencies=[Depends(require_key)])
    async def training_export(request: Request):
        """Every exchange with the user as training data: JSONL in chat format, one exchange per line, with feedback
        signals (whether the next message corrected her). Her learned lessons and skills come last as notes."""
        core: Core = request.app.state.core
        from iriis.agent.agenda import training_records

        def build() -> str:
            recs = training_records(core.db)
            notes = [{"note": r[1], "text": r[2]} for r in core.mind.rows(("lesson", "skill", "growth", "insight"), 1000)]
            return "\n".join(json.dumps(x, ensure_ascii=False) for x in [*recs, *notes]) + "\n"
        data = await asyncio.to_thread(build)
        return Response(data, media_type="application/x-ndjson",
                        headers={"Content-Disposition": 'attachment; filename="iriis-training.jsonl"'})

    @app.delete("/v1/mind/{item_id}", dependencies=[Depends(require_key)])
    async def mind_forget(item_id: int, request: Request):
        """Make her forget one thing (deleted, not hidden)."""
        core: Core = request.app.state.core
        await asyncio.to_thread(core.db.execute, "DELETE FROM mind WHERE id = ?", (item_id,))
        return {"ok": True}

    @app.post("/v1/confirm", dependencies=[Depends(require_key)])
    async def confirm(req: ConfirmRequest, request: Request):
        core: Core = request.app.state.core
        session = core.sessions.sessions.get(req.session_id)
        reply = req.text if req.text is not None else ("yes" if req.approved else "no")
        if not session or not session.resolve_confirmation(req.id, reply):
            raise HTTPException(404, "no pending confirmation")
        return {"ok": True}

    @app.delete("/v1/sessions/{session_id}", dependencies=[Depends(require_key)])
    async def reset_session(session_id: str, request: Request, purge: bool = False):
        """Start over (saved history stays searchable), or with ?purge=true delete it for good."""
        core: Core = request.app.state.core
        core.sessions.drop(session_id)
        await asyncio.to_thread(core.conversations.purge if purge else core.conversations.reset, session_id)
        return {"ok": True, "purged": purge}


    @app.post("/v1/tts", dependencies=[Depends(require_key)])
    async def tts(req: TTSRequest, request: Request):
        audio, sr = await request.app.state.core.speech.synthesize(req.text, req.voice)
        return Response(wav_bytes(audio, sr), media_type="audio/wav")

    @app.get("/v1/files/{file_id}", dependencies=[Depends(require_key)])
    async def get_file(file_id: int, request: Request):
        """A chart, diagram or document the studio made (for save_from_cloud, apps and other agents)."""
        import base64
        core: Core = request.app.state.core
        rows = await asyncio.to_thread(core.db.execute, "SELECT name, mime, data FROM files WHERE id = ?", (file_id,))
        if not rows:
            raise HTTPException(404, "no such file")
        name, mime, data = rows[0]
        return Response(base64.b64decode(data), media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/v1/settings/google", dependencies=[Depends(require_key)])
    async def google_settings(request: Request):
        """Store the Google OAuth client + refresh token (from `iriis google-login`) in the database."""
        body = await request.json()
        if not all(isinstance(body.get(k), str) and body[k] for k in ("client_id", "client_secret", "refresh_token")):
            raise HTTPException(400, "client_id, client_secret and refresh_token are required")
        core: Core = request.app.state.core
        val = json.dumps({k: body[k] for k in ("client_id", "client_secret", "refresh_token")})
        await asyncio.to_thread(core.db.execute,
                                "INSERT INTO kv (key, value, updated) VALUES ('google_oauth', ?, ?) "
                                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
                                (val, time.time()))
        try:
            import servers.google as g
            g._token.clear()
            account = (await asyncio.to_thread(g.google_status)).removeprefix("Connected as ").split(" (")[0]
        except Exception:  # noqa: BLE001
            account = ""
        return {"ok": True, "account": account}

    @app.post("/v1/stt", dependencies=[Depends(require_key)])
    async def stt(request: Request, file: UploadFile = File(...)):
        audio = decode_audio_file(await file.read())
        tr = await request.app.state.core.speech.transcribe(audio)
        return {"text": tr.text, "duration_s": round(tr.duration_s, 2), "elapsed_ms": round(tr.elapsed_s * 1000)}

    # ------------------------------------------------------------------ WebSockets

    @app.websocket("/v1/voice")
    async def voice(ws: WebSocket):
        """See llms.txt: binary PCM16 16 kHz mono in; JSON events + binary PCM16 audio out."""
        await ws.accept()
        if not await ws_authorised(ws, cfg.server.api_key_env):
            return
        core: Core = ws.app.state.core
        core.voice_sockets.add(ws)
        send_lock = asyncio.Lock()
        opts = {"session_id": None, "speak": True, "voice": None}
        buf = bytearray()
        session: Session | None = None
        turn: asyncio.Task | None = None

        async def emit(ev, audio):
            async with send_lock:
                await ws.send_json(ev)
                if audio is not None:
                    await ws.send_bytes(audio)

        async def start_turn(text: str, stt_ms: float | None = None):
            nonlocal turn
            timings = {"stt_ms": stt_ms} if stt_ms is not None else {}

            async def go():
                try:
                    await run_turn(session, text, emit, speech=core.speech, speak=opts["speak"], audio_format="pcm",
                                   voice=opts["voice"], confirm_timeout=core.cfg.server.confirm_timeout_s, timings=timings)
                except asyncio.CancelledError:
                    await emit({"type": "interrupted"}, None)
                except Exception as e:  # noqa: BLE001
                    log.exception("voice turn failed")
                    await emit({"type": "error", "message": str(e)}, None)

            turn = asyncio.create_task(go())

        def interrupt():
            if turn and not turn.done():
                turn.cancel()

        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    buf.extend(msg["bytes"])
                    if len(buf) > 16000 * 2 * 30:   # 30 s cap
                        del buf[: len(buf) - 16000 * 2 * 30]
                    continue
                data = json.loads(msg.get("text") or "{}")
                kind = data.get("type")
                if kind == "hello":
                    opts.update({k: data[k] for k in ("session_id", "speak", "voice") if k in data})
                    session = core.sessions.get(opts["session_id"])
                    await emit({"type": "ready", "session_id": session.id, "stt": core.speech.stt_name,
                                "tts": core.speech.tts_name, "output_sample_rate": 24000}, None)
                elif session is None:
                    await emit({"type": "error", "message": "send hello first"}, None)
                elif kind == "start_utterance":
                    buf.clear()
                elif kind == "end_utterance":
                    audio = from_pcm16(bytes(buf))
                    buf.clear()
                    if len(audio) < 16000 * 0.25:
                        await emit({"type": "transcript", "text": ""}, None)
                        continue
                    tr = await core.speech.transcribe(audio)
                    if tr.text and session.echo.is_echo(tr.text):
                        # Her own voice came back through the mic: not the user, so no reply.
                        await emit({"type": "transcript", "text": "", "ignored": "own voice", "heard": tr.text}, None)
                        continue
                    await emit({"type": "transcript", "text": tr.text, "stt_ms": round(tr.elapsed_s * 1000)}, None)
                    if not tr.text:
                        continue
                    if session.pending:             # this utterance answers a confirmation
                        session.resolve_confirmation(None, tr.text)
                        continue
                    if _is_stop(tr.text, cfg):
                        interrupt()
                        continue
                    interrupt()                     # a new request replaces anything still running
                    await start_turn(tr.text, tr.elapsed_s * 1000)
                elif kind == "text":
                    if session.pending:
                        session.resolve_confirmation(None, data.get("text", ""))
                        continue
                    interrupt()
                    await start_turn(data.get("text", ""))
                elif kind == "confirm_reply":
                    reply = data.get("text") if data.get("text") is not None else ("yes" if data.get("approved") else "no")
                    session.resolve_confirmation(data.get("id"), reply)
                elif kind == "wake":
                    core.speech.warm_up()            # "Hey Jarvis": you're definitely talking to her
                elif kind == "interrupt":
                    if session is not None and data.get("heard_seq") is not None:
                        session.heard_seq = int(data["heard_seq"])
                    interrupt()
                elif kind == "reset":
                    await asyncio.to_thread(core.conversations.reset, session.id)
                    core.sessions.drop(session.id)
                    session = core.sessions.get(session.id)
                    await emit({"type": "reset"}, None)
        except WebSocketDisconnect:
            pass
        finally:
            interrupt()
            core.voice_sockets.discard(ws)

    @app.websocket("/v1/desktop")
    async def desktop(ws: WebSocket):
        await ws.accept()
        if not await ws_authorised(ws, cfg.server.desktop_token_env):
            return
        await ws.app.state.core.desktop.attach(ws)

    app.mount("/", _MCPAuth(mcp_app, core_ref))  # serves /mcp (registered last, so API routes win)
    return app


def _is_stop(text: str, cfg: Config) -> bool:
    import re
    t = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return t in {w.lower() for w in cfg.barge_in.stop_words}


def _make_default_app() -> FastAPI:
    apply_offline_env()  # models are baked into the image; Hugging Face never needs the network
    from iriis.app import configure_logging
    configure_logging(stdout=True)
    return create_app()


app = _make_default_app() if os.environ.get("IRIIS_NO_DEFAULT_APP") != "1" else None
