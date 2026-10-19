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

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response

log = logging.getLogger("iriis.camera")
router = APIRouter()
CAMERA_SESSION = "iriis-app"          # one Vision session for the app, so goal/procedure/history persist
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "content-encoding", "authorization",
       "cookie", "upgrade"}


def vision_base() -> tuple[str, str]:
    """(https://vision-host, key) or ("", "") when the bench camera isn't configured."""
    url, key = os.environ.get("VISION_MCP_URL", "").strip(), os.environ.get("VISION_ACCESS_KEY", "").strip()
    if not url or not key:
        return "", ""
    return url.rstrip("/").removesuffix("/mcp"), key


def setup(require_key, ws_authorised, api_key_env: str, core_ref):
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
                    if d.get("type") == "answer" and not d.get("quiet") and not d.get("step_check"):
                        if last_q:
                            remember("user", last_q.pop(0))
                        remember("assistant", d.get("say", ""))
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
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() and not isinstance(t.exception(), (asyncio.CancelledError,)):
                    log.info("camera relay ended: %s", t.exception().__class__.__name__)
        finally:
            for t in tasks:
                t.cancel()
            await up.close()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    return router
