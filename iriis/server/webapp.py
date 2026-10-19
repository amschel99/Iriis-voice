"""The Iriis app (a PWA at /app): login, chat history, tasks, files, and Web Push notifications.

Push (free, standard Web Push): the VAPID key pair is generated once and kept in the database (kv), so there is no
setting to manage; subscriptions live in the `push_subs` table. Reminders, task reports and announcements go to
every subscribed phone or laptop, on the lock screen, even with the app closed (Android, desktop; iPhone once the
app is added to the home screen).
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import mimetypes
import re
import socket
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

log = logging.getLogger("iriis.webapp")
APP_DIR = Path(__file__).parent / "static" / "app"
COOKIE = "iriis_key"
WEB_SESSION = "owner"          # the app shares one conversation with voice and `iriis ask`
MAX_UPLOAD = 25 * 1024 * 1024  # photos, videos, voice notes and documents sent in the chat

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

    @router.get("/app/camera.js", include_in_schema=False)
    async def camera_js():
        return FileResponse(APP_DIR / "camera.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @router.get("/app/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(APP_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @router.get("/app/icon.svg", include_in_schema=False)
    async def icon():
        return FileResponse(APP_DIR / "icon.svg", media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    @router.get("/app/icon-{size}.png", include_in_schema=False)
    async def icon_png(size: str):
        f = APP_DIR / f"icon-{size}.png"           # 180, 192, 512, maskable-512 (the designed icon)
        if f.is_file():
            return FileResponse(f, media_type="image/png", headers={"Cache-Control": "max-age=86400"})
        try:
            n = int(size)
        except ValueError:
            raise HTTPException(404, "no such icon") from None
        return Response(_png_icon(min(max(n, 48), 512)), media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @router.get("/favicon.ico", include_in_schema=False)
    @router.get("/app/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(APP_DIR / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------------ data for the app
    @router.get("/v1/history", dependencies=deps)
    async def history(request: Request, session: str = WEB_SESSION, limit: int = 80):
        """The conversation so far (since the last 'start over'), oldest first, with times."""
        core = _core(request)

        def load():
            core.conversations.flush()
            rows = list(core.conversations._since_reset(session))[: max(1, min(limit, 500))]
            return [{"id": i, "role": r, "content": c, "ts": t} for i, r, c, t in reversed(rows)
                    if r in ("user", "assistant")]
        return await asyncio.to_thread(load)

    @router.post("/v1/history/delete", dependencies=deps)
    async def delete_messages(request: Request):
        """Delete messages permanently (for the user and for Iriis: her live context is reloaded without them)."""
        body = await request.json()
        session = str(body.get("session") or WEB_SESSION)
        try:
            ids = sorted({int(i) for i in body.get("ids") or []})[:500]
        except (TypeError, ValueError):
            raise HTTPException(400, "ids must be message ids")
        if not ids:
            return {"deleted": 0}
        core = _core(request)

        def run():
            core.conversations.flush()
            marks = ",".join("?" * len(ids))
            rows = core.db.execute(f"DELETE FROM messages WHERE id IN ({marks}) AND session_id = ? RETURNING id",
                                   (*ids, session))
            s = core.sessions.sessions.get(session)
            if s is not None:
                s.agent.history.messages = core.conversations.recent(session, core.cfg.llm.history_max_tokens)
            return len(rows)
        return {"deleted": await asyncio.to_thread(run)}

    # ------------------------------------------------------------------ files: uploads, metadata, streaming
    @router.post("/v1/upload", dependencies=deps)
    async def upload(request: Request, file: UploadFile = File(...)):
        """A photo, video, voice note or document the user sends in the chat; kept in the `files` table."""
        data = bytearray()
        while chunk := await file.read(1 << 20):
            data += chunk
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"files can be up to {MAX_UPLOAD // (1 << 20)} MB")
        name = Path(file.filename or "file").name[:200] or "file"
        mime = (file.content_type or "").split(";")[0].strip()
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(name)[0] or mime or "application/octet-stream"
        db = _core(request).db
        fid = await asyncio.to_thread(db.insert, "INSERT INTO files (name, mime, size, data, created) VALUES (?, ?, ?, ?, ?)",
                                      (name, mime, len(data), base64.b64encode(bytes(data)).decode(), time.time()))
        return {"id": fid, "name": name, "mime": mime, "size": len(data)}

    @router.get("/v1/files/{file_id}/meta", dependencies=deps)
    async def file_meta(file_id: int, request: Request):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "SELECT name, mime, size, created FROM files WHERE id = ?", (file_id,))
        if not rows:
            raise HTTPException(404, "no such file")
        name, mime, size, created = rows[0]
        return {"id": file_id, "name": name, "mime": mime, "size": size, "created": created}

    @router.get("/v1/media/{file_id}", dependencies=deps)
    async def media(file_id: int, request: Request, download: int = 0):
        """A stored file, inline, with HTTP Range support (so audio and video can seek)."""
        db = _core(request).db
        data = await asyncio.to_thread(_file_bytes, db, file_id)
        if data is None:
            raise HTTPException(404, "no such file")
        name, mime, raw = data
        quoted = urllib.parse.quote(name)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=86400",
                   "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quoted}"}
        total = len(raw)
        rng = request.headers.get("range", "")
        m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
            else:                                   # suffix range: the last N bytes
                start, end = max(0, total - int(m.group(2))), total - 1
            if start >= total or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            return Response(raw[start:end + 1], status_code=206, media_type=mime, headers=headers)
        return Response(raw, media_type=mime, headers=headers)

    @router.get("/v1/linkpreview", dependencies=deps)
    async def linkpreview(url: str):
        """Title, description and image of a web page (OpenGraph / Twitter cards), for link previews in the chat."""
        return await link_preview(url)

    # ------------------------------------------------------------------ calls from Iriis (alarms, reminders, her own)
    @router.get("/v1/calls", dependencies=deps)
    async def calls(request: Request, status: str = "ringing", limit: int = 20):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "SELECT id, at, reason, status, source FROM calls WHERE status = ? "
                                       "ORDER BY id DESC LIMIT ?", (status, max(1, min(limit, 100))))
        return [dict(zip(("id", "at", "reason", "status", "source"), r)) for r in rows]

    @router.get("/v1/calls/upcoming", dependencies=deps)
    async def calls_upcoming(request: Request, limit: int = 30):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "SELECT id, at, reason, status, source, repeat FROM calls WHERE "
                                       "status = 'scheduled' AND at >= ? ORDER BY at LIMIT ?",
                                       (time.time() - 60, max(1, min(limit, 100))))
        return [dict(zip(("id", "at", "reason", "status", "source", "repeat"), r)) for r in rows]

    @router.post("/v1/calls/{call_id}/answer", dependencies=deps)
    async def call_answer(call_id: int, request: Request):
        db = _core(request).db
        rows = await asyncio.to_thread(db.execute, "UPDATE calls SET status = 'answered', answered = ? WHERE id = ? "
                                       "AND status IN ('ringing', 'scheduled', 'missed') RETURNING id, reason, opening",
                                       (time.time(), call_id))
        if not rows:
            raise HTTPException(404, "that call isn't ringing")
        i, reason, opening = rows[0]
        return {"id": i, "reason": reason or "", "opening": opening or ""}

    async def _set_call(call_id: int, request: Request, status: str, allowed: tuple[str, ...]):
        db = _core(request).db
        marks = ",".join("?" * len(allowed))
        rows = await asyncio.to_thread(db.execute, f"UPDATE calls SET status = ? WHERE id = ? AND status IN ({marks}) "
                                       "RETURNING id", (status, call_id, *allowed))
        if not rows:
            raise HTTPException(404, "no such call")
        return {"ok": True}

    @router.post("/v1/calls/{call_id}/decline", dependencies=deps)
    async def call_decline(call_id: int, request: Request):
        return await _set_call(call_id, request, "declined", ("ringing", "scheduled"))

    @router.post("/v1/calls/{call_id}/cancel", dependencies=deps)
    async def call_cancel(call_id: int, request: Request):
        return await _set_call(call_id, request, "cancelled", ("scheduled", "ringing"))

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


async def push_all(db, title: str, body: str, url: str = "/app", data: dict | None = None) -> int:
    """Notify every subscribed device; subscriptions the browser has dropped (404/410) are removed.
    `data` is merged into the payload (e.g. {"kind": "call", "call_id": 7} makes the phone ring)."""
    try:
        rows = await asyncio.to_thread(db.execute, "SELECT endpoint, data FROM push_subs")
        if not rows:
            return 0
        priv, _ = await asyncio.to_thread(vapid_keys, db)
    except Exception as e:  # noqa: BLE001
        log.warning("push unavailable: %s", e)
        return 0
    payload = json.dumps({**(data or {}), "title": title, "body": body[:240], "url": url})
    sent = 0
    for endpoint, data in rows:
        code = await asyncio.to_thread(_send_one, json.loads(data), payload, priv)
        if code in (404, 410):
            await asyncio.to_thread(db.execute, "DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
        elif code < 300:
            sent += 1
    return sent


def _file_bytes(db, file_id: int) -> tuple[str, str, bytes] | None:
    rows = db.execute("SELECT name, mime, data FROM files WHERE id = ?", (file_id,))
    if not rows:
        return None
    name, mime, data = rows[0]
    return name, mime or "application/octet-stream", base64.b64decode(data)


# ---------------------------------------------------------------------------- link previews
_PREVIEWS: dict[str, tuple[float, dict]] = {}
PREVIEW_TTL, PREVIEW_MAX, PREVIEW_BYTES = 86400.0, 500, 512 * 1024
_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Mobile Safari/537.36")


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title, self._in_title, self.icon = "", False, ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower().strip()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "link" and "icon" in a.get("rel", "").lower() and not self.icon:
            self.icon = a.get("href", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def _public_host(host: str) -> bool:
    """True if every address the host resolves to is a public one (no loopback, private, link-local...)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def _safe_url(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname) and _public_host(u.hostname)


def parse_preview(url: str, html: str) -> dict:
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass
    m = p.meta

    def first(*keys):
        return next((m[k] for k in keys if m.get(k)), "")
    image = first("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src")
    if image:
        image = urllib.parse.urljoin(url, image)
        if not image.startswith(("http://", "https://")):
            image = ""
    host = urllib.parse.urlsplit(url).hostname or ""
    return {"url": url,
            "title": re.sub(r"\s+", " ", first("og:title", "twitter:title") or p.title).strip()[:200],
            "description": re.sub(r"\s+", " ", first("og:description", "twitter:description", "description")).strip()[:300],
            "image": image, "site": (first("og:site_name") or host.removeprefix("www."))[:80]}


async def link_preview(url: str) -> dict:
    url = (url or "").strip()[:2000]
    empty = {"url": url, "title": "", "description": "", "image": "", "site": urllib.parse.urlsplit(url).hostname or ""}
    hit = _PREVIEWS.get(url)
    if hit and time.time() - hit[0] < PREVIEW_TTL:
        return hit[1]
    out = empty
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=False,
                                     headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
                                              "Accept-Language": "en"}) as client:
            target = url
            for _ in range(6):                      # follow redirects by hand: every hop must be a public host
                if not await asyncio.to_thread(_safe_url, target):
                    return empty                    # refused: not cached either, so nothing is learned
                async with client.stream("GET", target) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        target = urllib.parse.urljoin(target, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        break
                    ctype = r.headers.get("content-type", "")
                    if ctype.startswith("image/"):
                        out = {**empty, "url": url, "image": target}
                        break
                    if "html" not in ctype and "xml" not in ctype:
                        break
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) >= PREVIEW_BYTES:
                            break
                    enc = r.encoding or "utf-8"
                    try:
                        html = buf.decode(enc, errors="replace")
                    except LookupError:
                        html = buf.decode("utf-8", errors="replace")
                    out = {**parse_preview(target, html), "url": url}
                    break
    except Exception as e:  # noqa: BLE001
        log.debug("link preview failed for %s: %s", url, e)
        out = empty
    if len(_PREVIEWS) >= PREVIEW_MAX:
        for k in sorted(_PREVIEWS, key=lambda k: _PREVIEWS[k][0])[: PREVIEW_MAX // 5]:
            _PREVIEWS.pop(k, None)
    _PREVIEWS[url] = (time.time(), out)
    return out


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
<title>Iriis</title><link rel="manifest" href="/app/manifest.webmanifest"><link rel="icon" href="/app/icon.svg" type="image/svg+xml"><link rel="icon" href="/app/favicon.ico" sizes="any"><link rel="apple-touch-icon" href="/app/icon-180.png">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1118;color:#e9eef4;
font:16px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}form{width:min(92vw,380px);display:grid;gap:14px;
background:#121b26;border:1px solid #1f2b3a;border-radius:18px;padding:26px}h1{margin:0;font-size:24px}
p{margin:0;color:#8ea0b5;line-height:1.45}input,button{font:inherit;padding:13px 14px;border-radius:12px;border:1px solid #233246;
background:#0b1118;color:#e9eef4}button{background:#3b82f6;border-color:#3b82f6;font-weight:600;cursor:pointer}
.err{color:#fbbf24}</style></head><body><form method="post" action="/app/login"><h1>Iriis</h1>
<p>Enter your Iriis key (IRIIS_API_KEY). This device will remember it.</p>{error}
<input name="key" type="password" placeholder="Iriis key" autocomplete="current-password" required autofocus>
<button>Open Iriis</button></form></body></html>"""
