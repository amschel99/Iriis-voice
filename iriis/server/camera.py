"""Camera mode in the Iriis app: the phone camera streams through Iriis to Seryn Vision (the bench assistant).

    WS  /v1/camera              the live link (frames up, answers/readings/alerts down), relayed to Vision's /ws
    ANY /v1/bench/{path}        Vision's HTTP API (uploads, video reviews, jobs, knowledge, procedures) via Iriis

One app, one login: Iriis holds the Vision key (VISION_ACCESS_KEY) and address (VISION_MCP_URL). What you ask on
camera and her answers are also written into your normal Iriis conversation, so chat remembers them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response

log = logging.getLogger("iriis.camera")
_BRAINS: set = set()
CAMERA_SESSION = "iriis-app"          # one Vision session for the app, so goal/procedure/history persist
DOING = {"studio": "making it…", "web": "searching the web…", "browser": "using your browser…", "whatsapp": "on WhatsApp…",
         "google": "checking Gmail and Calendar…", "seryn": "asking the Seryn Twin…", "tasks": "setting it up…",
         "notes_reminders": "making a note…", "memory": "remembering…", "bench": "looking again…",
         "documents": "working on the document…", "money": "checking your money…"}


def _file_meta(db, ids: list[str]) -> list[dict]:
    out = []
    for i in ids:
        rows = db.execute("SELECT id, name, mime FROM files WHERE id = ?", (int(i),))
        if rows:
            out.append({"id": rows[0][0], "name": rows[0][1], "type": rows[0][2]})
    return out
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "content-encoding", "authorization",
       "cookie", "upgrade"}


def vision_base() -> tuple[str, str]:
    """(https://vision-host, key) or ("", "") when the bench camera isn't configured."""
    url, key = os.environ.get("VISION_MCP_URL", "").strip(), os.environ.get("VISION_ACCESS_KEY", "").strip()
    if not url or not key:
        return "", ""
    return url.rstrip("/").removesuffix("/mcp"), key


def setup(require_key, ws_authorised, api_key_env: str, core_ref):
    router = APIRouter()          # one per app (tests build several apps in one process)
    deps = [Depends(require_key)]

    @router.get("/v1/camera/status", dependencies=deps)
    async def camera_status():
        base, _ = vision_base()
        if not base:
            return {"connected": False, "why": "Set VISION_MCP_URL and VISION_ACCESS_KEY on the Iriis service."}
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                h = (await c.get(f"{base}/api/health")).json()
            return {"connected": True, "model": h.get("model"), "note": h.get("note"), "ready": h.get("vlm_ready")}
        except Exception as e:  # noqa: BLE001
            return {"connected": False, "why": f"The bench service didn't answer ({e.__class__.__name__})."}

    @router.api_route("/v1/bench/{path:path}", methods=["GET", "POST", "DELETE"], dependencies=deps)
    async def bench_proxy(path: str, request: Request):
        base, key = vision_base()
        if not base:
            raise HTTPException(503, "The bench camera isn't connected (VISION_MCP_URL / VISION_ACCESS_KEY).")
        # a media file itself is /media/{id} on the bench service; everything else (incl. media/{id}/analyze) is /api
        import re
        target = f"{base}/{path}" if re.fullmatch(r"media/\d+", path) else f"{base}/api/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as c:
                r = await c.request(request.method, target, params=request.query_params, content=await request.body(),
                                    headers=headers)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"The bench service didn't answer ({e.__class__.__name__}).") from e
        return Response(r.content, status_code=r.status_code,
                        headers={k: v for k, v in r.headers.items() if k.lower() not in HOP})

    @router.websocket("/v1/camera")
    async def camera(ws: WebSocket):
        if not await ws_authorised(ws, api_key_env):
            return
        await ws.accept()
        base, key = vision_base()
        if not base:
            await ws.send_text(json.dumps({"type": "error", "fatal": True, "text":
                "The bench camera isn't connected yet. Add VISION_MCP_URL and VISION_ACCESS_KEY to the Iriis service on Railway."}))
            await ws.close(code=4404)
            return
        import websockets
        url = base.replace("https://", "wss://").replace("http://", "ws://") + f"/ws?session={CAMERA_SESSION}&key={key}"
        try:
            up = await websockets.connect(url, max_size=None, ping_interval=20, open_timeout=15)
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "error", "fatal": True,
                                           "text": f"Couldn't reach the bench service ({e.__class__.__name__}). Try again in a moment."}))
            await ws.close(code=1013)
            return
        last_q: list[str] = []
        try:       # what Iriis remembers and has learned about the user, so the camera knows who it's talking to
            core = core_ref.get("core")
            facts = [t for _, t in core.memory.all()[-40:]] if core and core.memory else []
            mind = core.mind.context("", for_camera=True) if core and getattr(core, "mind", None) else ""
            about = ("\n".join(f"- {f}" for f in facts) + ("\n\n" + mind if mind else "")).strip()
            if about:
                await up.send(json.dumps({"type": "about", "text": about[:4000]}))
        except Exception:  # noqa: BLE001
            pass
        brain_q: asyncio.Queue = asyncio.Queue()

        async def phone(msg: dict) -> None:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:  # noqa: BLE001
                pass

        async def brain():
            """The camera model answers fast; then her main brain decides whether there's more to do (make a PDF or
            a drawing, message someone, set a reminder, explain deeper) and does it with all her tools."""
            from iriis.server.sessions import run_turn
            while True:
                item = await brain_q.get()
                if item is None:
                    return
                question, said = item
                core = core_ref.get("core")
                if not core:
                    continue
                files: list[str] = []

                async def emit(ev, _audio):
                    kind = ev.get("type")
                    if kind == "tool":
                        t = ev.get("text", "")
                        files.extend(f for f in re.findall(r"\bready: file (\d+)\b", t) if f not in files)
                        m = re.match(r"^->\s+(\S+)", t)
                        if m:
                            await phone({"type": "brain_thinking", "text": DOING.get(m.group(1).split("__")[0], "working on it…")})
                    elif kind == "confirm":
                        await phone({"type": "brain_confirm", "id": ev["id"], "question": ev["question"],
                                     "session_id": ev["session_id"]})
                prompt = (f"[Video call: the user is showing you things through their phone camera.] They asked: \"{question}\"\n"
                          f"Your eyes (the camera model) already answered them out loud: \"{said}\"\n"
                          "If that fully answers them, reply with just the word SILENT. If they want more - something made "
                          "(a summary, PDF, drawing or sketch, plot, diagram), something done (a message, a reminder, a note, "
                          "a task), or a deeper explanation - do it now with your tools. camera_photo takes a picture from "
                          "the camera for documents; bench_look looks again. Keep what you say short: it's spoken.")
                try:
                    done = await run_turn(core.sessions.get("camera"), prompt, emit)
                except Exception as e:  # noqa: BLE001
                    log.info("camera brain failed: %s", e)
                    await phone({"type": "brain_done"})
                    continue
                text = (done.get("text") or "").strip()
                if not text and not files:
                    await phone({"type": "brain_done"})
                    continue
                meta = await asyncio.to_thread(_file_meta, core.db, files)
                await phone({"type": "brain", "say": text, "files": meta})
                remember("assistant", text + "".join(f"\n[file:{f}]" for f in files))

        def remember(role: str, text: str) -> None:
            core = core_ref.get("core")
            if core and text:
                try:
                    core.conversations.append("owner", role, f"[camera] {text}" if role == "user" else text)
                except Exception:  # noqa: BLE001
                    pass

        async def down():          # Vision -> phone
            async for m in up:
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                    continue
                try:
                    d = json.loads(m)
                    if d.get("type") == "answer" and d.get("noticed") and not d.get("quiet"):
                        q = f"(written in view) {d.get('question', '')}"   # she read a question and answered it
                        remember("user", q)
                        remember("assistant", d.get("say", ""))
                        brain_q.put_nowait((q, d.get("say", "")))
                    elif d.get("type") == "answer" and not d.get("quiet") and not d.get("step_check"):
                        q = last_q.pop(0) if last_q else ""
                        if q:
                            remember("user", q)
                        remember("assistant", d.get("say", ""))
                        if q and not d.get("coach"):
                            brain_q.put_nowait((q, d.get("say", "")))
                except ValueError:
                    pass
                await ws.send_text(m)

        async def upstream():      # phone -> Vision
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    return
                if m.get("bytes") is not None:
                    await up.send(m["bytes"])
                elif m.get("text") is not None:
                    try:
                        d = json.loads(m["text"])
                        if d.get("type") == "ask":
                            last_q.append(str(d.get("text", ""))[:500])
                    except ValueError:
                        pass
                    await up.send(m["text"])

        tasks = [asyncio.create_task(down()), asyncio.create_task(upstream())]
        thinker = asyncio.create_task(brain())
        _BRAINS.add(thinker)                  # kept alive until she's finished, even after the call ends
        thinker.add_done_callback(_BRAINS.discard)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() and not isinstance(t.exception(), (asyncio.CancelledError,)):
                    log.info("camera relay ended: %s", t.exception().__class__.__name__)
        finally:
            for t in tasks:
                t.cancel()
            brain_q.put_nowait(None)      # hung up: she still finishes what she's making (it lands in the chat), then stops
            await up.close()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    return router
