"""Ringing the owner's phone for free, even when it's locked and the app is closed: ntfy (https://ntfy.sh).

ntfy is a free, open-source push app (Android and iPhone, no account). Iriis publishes an urgent (priority 5)
alert to a private topic; with "keep alerting for max priority" switched on in the app, the phone rings and
vibrates like an alarm until it's tapped, and the tap opens Iriis on the call.

    NTFY_TOPIC=<a long random name only you know>   (anyone who knows it could send you alerts, so keep it secret)
    NTFY_SERVER=https://ntfy.sh                     (or your own ntfy server)
    IRIIS_PUBLIC_URL=https://<iriis host>           (for the "Answer" link; defaults to the Railway domain)
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("iriis.phone")


def configured() -> str:
    """'ntfy' when the free phone alarm is set up, else ''."""
    return "ntfy" if os.environ.get("NTFY_TOPIC", "").strip() else ""


def _app_url(call_id: int | None) -> str:
    base = (os.environ.get("IRIIS_PUBLIC_URL") or
            (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")
    return f"{base}/app?call={call_id}&answer=1" if base and call_id else (f"{base}/app" if base else "")


async def call(reason: str, opening: str = "", call_id: int | None = None) -> str:
    """Ring the phone through ntfy. Returns 'ntfy', or '' when it isn't set up; raises if ntfy refused."""
    if not configured():
        return ""
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    topic = os.environ["NTFY_TOPIC"].strip()
    url = _app_url(call_id)
    headers = {"Title": "Iriis is calling", "Priority": "5", "Tags": "telephone_receiver,rotating_light"}
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Answer, {url}, clear=true"
    body = (opening.strip() or reason.strip() or "Iriis is calling you.")[:400]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{server}/{topic}", content=body.encode("utf-8"), headers=headers)
    if r.status_code >= 300:
        raise RuntimeError(f"ntfy refused the alert: HTTP {r.status_code} {r.text[:200]}")
    return "ntfy"
