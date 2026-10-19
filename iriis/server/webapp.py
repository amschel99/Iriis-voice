"""The Iriis app (a PWA at /app): login, chat history, tasks, files, and Web Push notifications.

Push (free, standard Web Push): the VAPID key pair is generated once and kept in the database (kv), so there is no
setting to manage; subscriptions live in the `push_subs` table. Reminders, task reports and announcements go to
every subscribed phone or laptop, on the lock screen, even with the app closed (Android, desktop; iPhone once the
app is added to the home screen).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

log = logging.getLogger("iriis.webapp")
APP_DIR = Path(__file__).parent / "static" / "app"
COOKIE = "iriis_key"
WEB_SESSION = "owner"          # the app shares one conversation with voice and `iriis ask`

router = APIRouter()


def _core(request: Request):
    return request.app.state.core


def setup(require_key):
    """Routes need the app's key check; app.py passes it in (avoids an import cycle)."""
    deps = [Depends(require_key)]

    @router.get("/app", include_in_schema=False)
    @router.get("/app/", include_in_schema=False)
    async def app_page(request: Request):
        from iriis.server.app import _check, _secret, _token_from
        core = _core(request)
        if not _check(_token_from(request.headers, request.query_params), _secret(core.cfg.server.api_key_env),
                      request.client.host if request.client else None):
            return RedirectResponse("/app/login", status_code=303)
        return FileResponse(APP_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @router.get("/app/login", include_in_schema=False)
    async def login_page():
        return HTMLResponse(LOGIN_HTML.replace("{error}", ""))

    @router.post("/app/login", include_in_schema=False)
    async def do_login(request: Request):
        from iriis.server.app import _check, _secret
        form = await request.form()
        key = str(form.get("key", "")).strip()
        core = _core(request)
        if not key or not _check(key, _secret(core.cfg.server.api_key_env), None):
            return HTMLResponse(LOGIN_HTML.replace("{error}", '<p class="err">That key is wrong.</p>'), status_code=401)
        resp = RedirectResponse("/app", status_code=303)
        resp.set_cookie(COOKIE, key, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return resp

    @router.get("/app/logout", include_in_schema=False)
    async def logout():
        resp = RedirectResponse("/app/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # the app's own files (no key needed: they contain nothing private)
    @router.get("/app/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(APP_DIR / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @router.get("/app/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(APP_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @router.get("/app/icon.svg", include_in_schema=False)
    async def icon():
        return FileResponse(APP_DIR / "icon.svg", media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    @router.get("/app/icon-{size}.png", include_in_schema=False)
    async def icon_png(size: int):
        return Response(_png_icon(min(max(size, 48), 512)), media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------------ data for the app
    @router.get("/v1/history", dependencies=deps)
    async def history(request: Request, session: str = WEB_SESSION, limit: int = 80):
        """The conversation so far (since the last 'start over'), oldest first, with times."""
        core = _core(request)

        def load():
            core.conversations.flush()
            rows = list(core.conversations._since_reset(session))[: max(1, min(limit, 500))]
            return [{"role": r, "content": c, "ts": t} for _, r, c, t in reversed(rows) if r in ("user", "assistant")]
        return await asyncio.to_thread(load)

    @router.get("/v1/tasks", dependencies=deps)
    async def tasks(request: Request, limit: int = 40):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "SELECT id, title, goal, repeat, status, next_run, last_run, runs, result, "
                                       "created FROM tasks ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
        keys = ("id", "title", "goal", "repeat", "status", "next_run", "last_run", "runs", "result", "created")
        return [dict(zip(keys, r)) for r in rows]

    @router.post("/v1/tasks/{task_id}/cancel", dependencies=deps)
    async def cancel_task(task_id: int, request: Request):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "UPDATE tasks SET status = 'cancelled', next_run = NULL WHERE id = ? "
                                       "AND status IN ('scheduled', 'running', 'failed') RETURNING id", (task_id,))
        if not rows:
            raise HTTPException(404, "not scheduled or running")
        return {"ok": True}

    @router.post("/v1/tasks/{task_id}/run", dependencies=deps)
    async def run_task_now(task_id: int, request: Request):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "UPDATE tasks SET status = 'scheduled', next_run = ? WHERE id = ? "
                                       "AND status <> 'running' RETURNING id", (time.time(), task_id))
        if not rows:
            raise HTTPException(404, "already running or not found")
        return {"ok": True}

    # ------------------------------------------------------------------ push notifications
    @router.get("/v1/push/key", dependencies=deps)
    async def push_key(request: Request):
        return {"key": await asyncio.to_thread(vapid_public_key, _core(request).db)}

    @router.post("/v1/push/subscribe", dependencies=deps)
    async def push_subscribe(request: Request):
        sub = await request.json()
        if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://") or "keys" not in sub:
            raise HTTPException(400, "not a push subscription")
        db = _core(request).db
        await asyncio.to_thread(db.execute, "INSERT INTO push_subs (endpoint, data, created) VALUES (?, ?, ?) "
                                "ON CONFLICT (endpoint) DO UPDATE SET data = excluded.data", (sub["endpoint"], json.dumps(sub), time.time()))
        return {"ok": True}

    @router.post("/v1/push/unsubscribe", dependencies=deps)
    async def push_unsubscribe(request: Request):
        sub = await request.json()
        db = _core(request).db
        await asyncio.to_thread(db.execute, "DELETE FROM push_subs WHERE endpoint = ?", (str(sub.get("endpoint", "")),))
        return {"ok": True}

    @router.post("/v1/push/test", dependencies=deps)
    async def push_test(request: Request):
        n = await push_all(_core(request).db, "Iriis", "Notifications work. I'll tell you here when tasks finish.")
        return {"sent": n}

    return router


# ---------------------------------------------------------------------------- VAPID + sending
def vapid_keys(db) -> tuple[str, str]:
    """(private PEM, public key base64url): made once, kept in the database."""
    rows = db.execute("SELECT value FROM kv WHERE key = 'vapid'")
    if rows:
        d = json.loads(rows[0][0])
        return d["private"], d["public"]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    db.execute("INSERT INTO kv (key, value, updated) VALUES ('vapid', ?, ?) ON CONFLICT (key) DO NOTHING",
               (json.dumps({"private": priv, "public": pub_b64}), time.time()))
    return vapid_keys(db) if not rows else (priv, pub_b64)


def vapid_public_key(db) -> str:
    return vapid_keys(db)[1]


def _send_one(sub: dict, payload: str, private_pem: str) -> int:
    from pywebpush import WebPushException, webpush
    try:
        webpush(subscription_info=sub, data=payload, vapid_private_key=private_pem,
                vapid_claims={"sub": "mailto:iriis@localhost"}, ttl=3600)
        return 200
    except WebPushException as e:
        return getattr(e.response, "status_code", 500) or 500


async def push_all(db, title: str, body: str, url: str = "/app") -> int:
    """Notify every subscribed device; subscriptions the browser has dropped (404/410) are removed."""
    try:
        rows = await asyncio.to_thread(db.execute, "SELECT endpoint, data FROM push_subs")
        if not rows:
            return 0
        priv, _ = await asyncio.to_thread(vapid_keys, db)
    except Exception as e:  # noqa: BLE001
        log.warning("push unavailable: %s", e)
        return 0
    payload = json.dumps({"title": title, "body": body[:240], "url": url})
    sent = 0
    for endpoint, data in rows:
        code = await asyncio.to_thread(_send_one, json.loads(data), payload, priv)
        if code in (404, 410):
            await asyncio.to_thread(db.execute, "DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
        elif code < 300:
            sent += 1
    return sent


def _png_icon(size: int) -> bytes:
    """A plain app icon (blue square, white ring) for home screens that need PNG."""
    import io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (size, size), (46, 134, 193))
    d = ImageDraw.Draw(im)
    r, w = size * 0.26, max(2, size // 12)
    c = size / 2
    d.ellipse((c - r, c - r, c + r, c + r), outline=(255, 255, 255), width=w)
    d.ellipse((c - size * 0.05, c - size * 0.05, c + size * 0.05, c + size * 0.05), fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#0b1118">
<title>Iriis</title><link rel="manifest" href="/app/manifest.webmanifest"><link rel="icon" href="/app/icon.svg">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1118;color:#e9eef4;
font:16px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}form{width:min(92vw,380px);display:grid;gap:14px;
background:#121b26;border:1px solid #1f2b3a;border-radius:18px;padding:26px}h1{margin:0;font-size:24px}
p{margin:0;color:#8ea0b5;line-height:1.45}input,button{font:inherit;padding:13px 14px;border-radius:12px;border:1px solid #233246;
background:#0b1118;color:#e9eef4}button{background:#3b82f6;border-color:#3b82f6;font-weight:600;cursor:pointer}
.err{color:#fbbf24}</style></head><body><form method="post" action="/app/login"><h1>Iriis</h1>
<p>Enter your Iriis key (IRIIS_API_KEY). This device will remember it.</p>{error}
<input name="key" type="password" placeholder="Iriis key" autocomplete="current-password" required autofocus>
<button>Open Iriis</button></form></body></html>"""
