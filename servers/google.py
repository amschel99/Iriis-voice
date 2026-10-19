"""google MCP server: Gmail and Google Calendar through Google's own (free) REST APIs.

Runs in the cloud core. The OAuth client and refresh token live in the database (`kv` table, written by
`iriis google-login`), or in GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN, so nothing is stored
on the PC. Scopes: gmail.modify (read, label, draft, send) and calendar.

Reading, searching and drafting are harmless and run directly; sending mail, archiving, and creating, changing
or deleting events need the user's confirmation (tiers in config/mcp_servers.yaml).
Email bodies are untrusted content: Iriis treats them as data, never as instructions.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import time
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from zoneinfo import ZoneInfo

import httpx

from servers._common import make_server

mcp = make_server("google")
TZ_NAME = os.environ.get("IRIIS_TIMEZONE", "Africa/Nairobi")
TZ = ZoneInfo(TZ_NAME)
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar"]
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
TIMEOUT = httpx.Timeout(30, connect=10)
_token: dict = {}

NOT_SET_UP = ("Google isn't connected yet. On your computer run: iriis google-login path\\to\\client_secret.json "
              "(the free OAuth client from console.cloud.google.com; see docs/google.md).")


# ---------------------------------------------------------------------------- credentials
def _creds() -> dict:
    env = {k: os.environ.get(f"GOOGLE_{k.upper()}", "") for k in ("client_id", "client_secret", "refresh_token")}
    if all(env.values()):
        return env
    try:
        from iriis.storage import get_db
        rows = get_db().execute("SELECT value FROM kv WHERE key = 'google_oauth'")
        if rows:
            return json.loads(rows[0][0])
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(NOT_SET_UP)


def _access_token() -> str:
    if _token.get("expires", 0) > time.time() + 60:
        return _token["access_token"]
    c = _creds()
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=TIMEOUT, data={
        "client_id": c["client_id"], "client_secret": c["client_secret"], "refresh_token": c["refresh_token"],
        "grant_type": "refresh_token"})
    if r.status_code != 200:
        err = r.json().get("error", r.status_code) if r.headers.get("content-type", "").startswith("application/json") else r.status_code
        if err == "invalid_grant":
            raise RuntimeError("Google sign-in expired or was revoked. Run iriis google-login again (and set the OAuth "
                               "app to 'In production' so it stops expiring every 7 days).")
        raise RuntimeError(f"Google token refresh failed ({err}).")
    d = r.json()
    _token.update(access_token=d["access_token"], expires=time.time() + d.get("expires_in", 3600))
    return d["access_token"]


def _req(method: str, url: str, **kw) -> dict:
    r = httpx.request(method, url, timeout=TIMEOUT, headers={"Authorization": f"Bearer {_access_token()}"}, **kw)
    if r.status_code == 204:
        return {}
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:200])
        except ValueError:
            msg = r.text[:200]
        raise RuntimeError(f"Google said {r.status_code}: {msg}")
    return r.json() if r.content else {}


# ---------------------------------------------------------------------------- gmail helpers
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def body_text(payload: dict) -> str:
    """Plain text of a Gmail message payload (text/plain preferred, else HTML stripped)."""
    plain, html_ = [], []

    def walk(p):
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt == "text/plain":
            plain.append(_b64d(data).decode("utf-8", "replace"))
        elif data and mt == "text/html":
            html_.append(_b64d(data).decode("utf-8", "replace"))
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    elif html_:
        from servers.web import html_to_text
        text = html_to_text("\n".join(html_))[1]
    else:
        text = ""
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r", ""))
    # drop quoted history ("On Mon, X wrote:" and "> " lines) so replies read short
    text = re.split(r"\n(?:On .{5,120} wrote:|-----Original Message-----)\s*\n", text, maxsplit=1)[0]
    return "\n".join(l for l in text.split("\n") if not l.startswith(">")).strip()


def _when(ts_ms: str | int) -> str:
    d = dt.datetime.fromtimestamp(int(ts_ms) / 1000, TZ)
    today = dt.datetime.now(TZ).date()
    return d.strftime("%H:%M") if d.date() == today else d.strftime("%a %d %b %H:%M")


def _who(h: str) -> str:
    name, addr = parseaddr(h)
    return name or addr


def _attachments(payload: dict) -> list[str]:
    out = []

    def walk(p):
        if p.get("filename"):
            out.append(p["filename"])
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    return out


# ---------------------------------------------------------------------------- gmail tools
@mcp.tool()
def gmail_search(query: str = "is:unread in:inbox", max_results: int = 8) -> str:
    """Search Gmail with Gmail's own syntax: "is:unread", "from:jane", "subject:invoice newer_than:7d",
    "has:attachment", "in:sent". Returns id, sender, subject, time and a snippet for each message."""
    d = _req("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": max(1, min(max_results, 25))})
    ids = [m["id"] for m in d.get("messages", [])]
    if not ids:
        return f"No emails match '{query}'."
    rows = []
    for mid in ids:
        m = _req("GET", f"{GMAIL}/messages/{mid}", params={"format": "metadata", "metadataHeaders": ["From", "Subject"]})
        h = _headers(m)
        unread = "UNREAD" in m.get("labelIds", [])
        rows.append(f"[{mid}] {_who(h.get('from', ''))}: {h.get('subject', '(no subject)')} ({_when(m['internalDate'])}"
                    f"{', unread' if unread else ''})\n   {m.get('snippet', '')[:160]}")
    total = d.get("resultSizeEstimate", len(ids))
    return "\n".join(rows) + (f"\n(about {total} match; showing {len(ids)})" if total > len(ids) else "")


@mcp.tool()
def gmail_read(message_id: str, max_chars: int = 6000) -> str:
    """Read one email (sender, recipients, subject, date, attachments' names and the text, quoted history removed)."""
    m = _req("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"})
    h = _headers(m)
    att = _attachments(m.get("payload", {}))
    text = body_text(m.get("payload", {})) or m.get("snippet", "")
    head = (f"From: {h.get('from', '')}\nTo: {h.get('to', '')}" + (f"\nCc: {h['cc']}" if h.get("cc") else "") +
            f"\nSubject: {h.get('subject', '')}\nDate: {_when(m['internalDate'])}\nThread: {m.get('threadId')}" +
            (f"\nAttachments: {', '.join(att)}" if att else ""))
    more = f"\n[... {len(text) - max_chars} more characters]" if len(text) > max_chars else ""
    return f"{head}\n\n{text[:max_chars]}{more}"


def _raw(to: str, subject: str, body: str, cc: str = "", reply_to: dict | None = None) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    out = {}
    if reply_to:
        msg["In-Reply-To"] = reply_to["message_id"]
        msg["References"] = (reply_to.get("references", "") + " " + reply_to["message_id"]).strip()
        out["threadId"] = reply_to["thread_id"]
    msg.set_content(body)
    out["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return out


def _reply_context(message_id: str) -> tuple[dict, dict]:
    m = _req("GET", f"{GMAIL}/messages/{message_id}", params={"format": "metadata",
             "metadataHeaders": ["From", "Reply-To", "To", "Cc", "Subject", "Message-ID", "References"]})
    h = _headers(m)
    return h, {"message_id": h.get("message-id", ""), "references": h.get("references", ""), "thread_id": m["threadId"]}


@mcp.tool()
def gmail_draft(to: str = "", subject: str = "", body: str = "", cc: str = "", reply_to_message_id: str = "",
                reply_all: bool = False) -> str:
    """Write an email as a DRAFT (nothing is sent). For a reply give reply_to_message_id (to/subject are filled in
    from the original). Returns the draft id: read it back to the user, then gmail_send_draft after they agree."""
    ctx = None
    if reply_to_message_id:
        h, ctx = _reply_context(reply_to_message_id)
        if not to:
            to = h.get("reply-to") or h.get("from", "")
            if reply_all:
                me = _req("GET", f"{GMAIL}/profile")["emailAddress"].lower()
                others = [a for _, a in getaddresses([h.get("to", ""), h.get("cc", "")]) if a and a.lower() != me]
                cc = ", ".join([cc] + others if cc else others)
        subj = h.get("subject", "")
        subject = subject or (subj if subj.lower().startswith("re:") else f"Re: {subj}")
    if not to or not body:
        raise ValueError("A draft needs a recipient and a body.")
    d = _req("POST", f"{GMAIL}/drafts", json={"message": _raw(to, subject, body, cc, ctx)})
    return f"Draft saved (draft id {d['id']}) to {to}" + (f", cc {cc}" if cc else "") + f": '{subject}'. Not sent yet."


@mcp.tool()
def gmail_send_draft(draft_id: str) -> str:
    """Send a draft created with gmail_draft (only after the user agreed to send it)."""
    d = _req("POST", f"{GMAIL}/drafts/send", json={"id": draft_id})
    return f"Sent (message id {d.get('id')})."


@mcp.tool()
def gmail_mark(message_id: str, read: bool = True, archive: bool = False, star: bool | None = None) -> str:
    """Mark an email read/unread, archive it (remove from inbox) or star it."""
    add, remove = [], []
    (remove if read else add).append("UNREAD")
    if archive:
        remove.append("INBOX")
    if star is not None:
        (add if star else remove).append("STARRED")
    _req("POST", f"{GMAIL}/messages/{message_id}/modify", json={"addLabelIds": add, "removeLabelIds": remove})
    return "Done: " + ", ".join(x for x in ["read" if read else "unread", "archived" if archive else "",
                                            {True: "starred", False: "unstarred", None: ""}[star]] if x) + "."


# ---------------------------------------------------------------------------- calendar
def parse_dt(s: str, now: dt.datetime | None = None) -> dt.datetime:
    """'2026-10-20 14:00', 'tomorrow 9am', 'friday 15:30', 'today 17:00', 'in 2 hours'."""
    now = now or dt.datetime.now(TZ)
    t = s.strip().lower().replace(" at ", " ")
    try:
        d = dt.datetime.fromisoformat(s.strip())
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except ValueError:
        pass
    m = re.fullmatch(r"in (\d+(?:\.\d+)?) ?(minutes?|mins?|hours?|hrs?|days?)", t)
    if m:
        n, u = float(m.group(1)), m.group(2)
        return now + dt.timedelta(minutes=n) if u.startswith("m") else now + dt.timedelta(hours=n) if u.startswith("h") else now + dt.timedelta(days=n)
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    base = now.date()
    parts = t.split(" ", 1)
    if parts[0] in ("today", "tomorrow") or parts[0] in days or parts[0] in [d[:3] for d in days]:
        if parts[0] == "tomorrow":
            base = base + dt.timedelta(days=1)
        elif parts[0] != "today":
            wd = [d[:3] for d in days].index(parts[0][:3])
            base = base + dt.timedelta(days=(wd - base.weekday()) % 7 or 7)
        t = parts[1] if len(parts) > 1 else "09:00"
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?", t.strip())
    if not m:
        raise ValueError(f"Couldn't read the time '{s}'. Try '2026-10-20 14:00' or 'tomorrow 9am'.")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3).startswith("p") else 0)
    return dt.datetime(base.year, base.month, base.day, h, mi, tzinfo=TZ)


def _ev_line(e: dict) -> str:
    st = e.get("start", {})
    if "date" in st:
        when = dt.date.fromisoformat(st["date"]).strftime("%a %d %b") + " (all day)"
    else:
        a = dt.datetime.fromisoformat(st["dateTime"]).astimezone(TZ)
        b = dt.datetime.fromisoformat(e["end"]["dateTime"]).astimezone(TZ)
        when = f"{a.strftime('%a %d %b %H:%M')}-{b.strftime('%H:%M')}"
    extra = f" at {e['location']}" if e.get("location") else ""
    n = len(e.get("attendees", []))
    return f"[{e['id']}] {when}: {e.get('summary', '(no title)')}{extra}" + (f" ({n} guests)" if n else "")


@mcp.tool()
def calendar_events(days: int = 7, query: str = "", start: str = "today 00:00") -> str:
    """Upcoming events on the user's Google Calendar: the next `days` days from `start`, optionally matching `query`."""
    a = parse_dt(start)
    b = a + dt.timedelta(days=max(1, min(days, 90)))
    params = {"timeMin": a.isoformat(), "timeMax": b.isoformat(), "singleEvents": "true", "orderBy": "startTime",
              "maxResults": 50, "timeZone": TZ_NAME}
    if query:
        params["q"] = query
    items = _req("GET", f"{CAL}/calendars/primary/events", params=params).get("items", [])
    return "\n".join(_ev_line(e) for e in items) if items else f"Nothing on the calendar in those {days} days."


@mcp.tool()
def calendar_free_time(day: str = "today", start_hour: int = 8, end_hour: int = 18, min_minutes: int = 30) -> str:
    """Free slots on a day between start_hour and end_hour (e.g. to suggest meeting times)."""
    d = parse_dt(f"{day} 00:00") if not re.search(r"\d{1,2}[:.]\d{2}", day) else parse_dt(day)
    a = d.replace(hour=start_hour, minute=0)
    b = d.replace(hour=end_hour, minute=0)
    fb = _req("POST", f"{CAL}/freeBusy", json={"timeMin": a.isoformat(), "timeMax": b.isoformat(), "timeZone": TZ_NAME,
                                               "items": [{"id": "primary"}]})
    busy = [(dt.datetime.fromisoformat(x["start"]).astimezone(TZ), dt.datetime.fromisoformat(x["end"]).astimezone(TZ))
            for x in fb["calendars"]["primary"]["busy"]]
    free, cur = [], max(a, dt.datetime.now(TZ)) if a.date() == dt.datetime.now(TZ).date() else a
    for s, e in sorted(busy):
        if (s - cur).total_seconds() >= min_minutes * 60:
            free.append((cur, s))
        cur = max(cur, e)
    if (b - cur).total_seconds() >= min_minutes * 60:
        free.append((cur, b))
    if not free:
        return f"No free {min_minutes}-minute slots on {a.strftime('%A %d %B')} between {start_hour}:00 and {end_hour}:00."
    return f"Free on {a.strftime('%A %d %B')}: " + ", ".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in free)


@mcp.tool()
def calendar_create(title: str, start: str, end: str = "", duration_minutes: int = 60, location: str = "",
                    description: str = "", attendees: str = "", all_day: bool = False) -> str:
    """Add an event to the user's Google Calendar. start like 'tomorrow 9am' or '2026-10-20 14:00'; attendees is a
    comma-separated list of emails (Google emails them an invitation)."""
    s = parse_dt(start)
    if all_day:
        body = {"start": {"date": s.date().isoformat()},
                "end": {"date": (parse_dt(end).date() if end else s.date() + dt.timedelta(days=1)).isoformat()}}
    else:
        e = parse_dt(end) if end else s + dt.timedelta(minutes=duration_minutes)
        body = {"start": {"dateTime": s.isoformat(), "timeZone": TZ_NAME}, "end": {"dateTime": e.isoformat(), "timeZone": TZ_NAME}}
    body.update(summary=title, location=location, description=description)
    guests = [a.strip() for a in attendees.split(",") if a.strip()]
    if guests:
        body["attendees"] = [{"email": a} for a in guests]
    ev = _req("POST", f"{CAL}/calendars/primary/events", json=body, params={"sendUpdates": "all" if guests else "none"})
    return "Added: " + _ev_line(ev)


@mcp.tool()
def calendar_update(event_id: str, title: str = "", start: str = "", end: str = "", location: str = "",
                    description: str = "") -> str:
    """Change an event (only the fields given). Guests get an update email."""
    ev = _req("GET", f"{CAL}/calendars/primary/events/{event_id}")
    patch: dict = {}
    if title:
        patch["summary"] = title
    if location:
        patch["location"] = location
    if description:
        patch["description"] = description
    if start:
        s = parse_dt(start)
        old = ev.get("start", {}).get("dateTime")
        length = (dt.datetime.fromisoformat(ev["end"]["dateTime"]) - dt.datetime.fromisoformat(old)) if old else dt.timedelta(hours=1)
        e = parse_dt(end) if end else s + length
        patch["start"] = {"dateTime": s.isoformat(), "timeZone": TZ_NAME}
        patch["end"] = {"dateTime": e.isoformat(), "timeZone": TZ_NAME}
    elif end:
        patch["end"] = {"dateTime": parse_dt(end).isoformat(), "timeZone": TZ_NAME}
    if not patch:
        return "Nothing to change."
    ev = _req("PATCH", f"{CAL}/calendars/primary/events/{event_id}", json=patch, params={"sendUpdates": "all"})
    return "Updated: " + _ev_line(ev)


@mcp.tool()
def calendar_delete(event_id: str) -> str:
    """Delete an event from the user's calendar (guests are told it's cancelled)."""
    _req("DELETE", f"{CAL}/calendars/primary/events/{event_id}", params={"sendUpdates": "all"})
    return "Deleted."


@mcp.tool()
def google_status() -> str:
    """Whether Gmail and Calendar are connected, and as which account."""
    try:
        me = _req("GET", f"{GMAIL}/profile")
    except RuntimeError as e:
        return str(e)
    return f"Connected as {me['emailAddress']} ({me.get('messagesTotal', 0):,} emails)."


if __name__ == "__main__":
    mcp.run()
