"""OAuth 2.1 sign-in for an MCP server, so Claude (claude.ai, Desktop, mobile) and other MCP clients connect with no key.

Claude: Settings -> Connectors -> Add custom connector -> https://<host>/mcp (leave the OAuth fields empty). It finds
the sign-in at /.well-known, registers itself (RFC 7591) or names
itself by a URL that publishes its details (CIMD, Claude's default), sends the owner to an "Allow?" page that asks for this
app's password, proves the request with PKCE (S256), and gets a one-hour access token plus a rotating refresh token.
API keys keep working. Only hashes of codes and tokens are stored, in one JSON document (store_load/store_save).

One file, plain Starlette, shared by Iriis Voice, Seryn Twin and Seryn Vision (adapted from Iriis HQ's sign-in).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from html import escape
from typing import Callable
from urllib.parse import urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

ACCESS_TTL = 60 * 60
REFRESH_TTL = 180 * 24 * 60 * 60
CODE_TTL = 600
OPEN = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
PARAMS = ("client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method", "state", "scope",
          "resource")


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status


def redirect_ok(uri: str) -> bool:
    """https anywhere (Claude: https://claude.ai/api/mcp/auth_callback), or http on this machine."""
    p = urlparse(uri or "")
    if p.fragment or not p.netloc:
        return False
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"))


def base_url(headers, scheme: str = "https") -> str:
    proto = headers.get("x-forwarded-proto") or scheme
    host = headers.get("x-forwarded-host") or headers.get("host") or "localhost"
    return f"{proto.split(',')[0].strip()}://{host}"


class MCPOAuth:
    def __init__(self, name: str, prefix: str, store_load: Callable[[], str | None], store_save: Callable[[str], None],
                 check_password: Callable[[str], bool], password_label: str, mcp_path: str = "/mcp"):
        self.name, self.prefix, self.mcp_path = name, prefix, mcp_path
        self._load_raw, self._save_raw, self.check_password = store_load, store_save, check_password
        self.password_label = password_label
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._cimd: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------ storage
    def _hash(self, raw: str) -> str:
        return hashlib.sha256(f"{self.prefix}-oauth-v1:{raw}".encode()).hexdigest()

    def _load(self) -> dict:
        raw = self._load_raw()
        data = json.loads(raw) if raw else {}
        for k in ("clients", "codes", "tokens"):
            data.setdefault(k, [])
        return data

    def _save(self, data: dict) -> None:
        now = time.time()
        data["codes"] = [c for c in data["codes"] if c["expires"] > now]
        data["tokens"] = [t for t in data["tokens"] if t["refresh_expires"] > now]
        self._save_raw(json.dumps(data))

    # ------------------------------------------------------------------ the protocol
    def register(self, meta: dict) -> dict:
        uris = meta.get("redirect_uris") or []
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and redirect_ok(u) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be https URLs (or http://localhost)")
        method = meta.get("token_endpoint_auth_method") or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError("invalid_client_metadata", f"token_endpoint_auth_method {method} isn't supported")
        client = {"client_id": f"{self.prefix}_client_" + secrets.token_urlsafe(16),
                  "client_id_issued_at": int(time.time()),
                  "client_name": str(meta.get("client_name") or "MCP client")[:120], "redirect_uris": uris[:10],
                  "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"]}
        secret = secrets.token_urlsafe(32) if method != "none" else ""
        if secret:
            client["client_secret_expires_at"] = 0
        with self._lock:
            data = self._load()
            data["clients"] = [*data["clients"], {**client, "secret_hash": self._hash(secret) if secret else ""}][-200:]
            self._save(data)
        return {**client, **({"client_secret": secret} if secret else {})}

    def client(self, client_id: str, data: dict | None = None) -> dict | None:
        """A registered app, or one that publishes its own details at its client_id URL (CIMD, e.g. Claude)."""
        found = next((c for c in (data or self._load())["clients"] if c["client_id"] == client_id), None)
        return found or (self._metadata_client(client_id) if client_id.startswith("https://") else None)

    def _metadata_client(self, url: str) -> dict | None:
        """Client ID Metadata Document: fetch the JSON at the client_id URL (cached an hour). Public https hosts only."""
        hit = self._cimd.get(url)
        if hit and hit[0] > time.time():
            return hit[1]
        import ipaddress
        import socket
        import urllib.request
        host = urlparse(url).hostname or ""
        try:
            for info in socket.getaddrinfo(host, 443):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return None                       # never fetch from inside the network
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"{self.name} MCP"})
            with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310  (https only, public hosts only)
                meta = json.loads(r.read(64_000).decode())
        except Exception:  # noqa: BLE001
            return None
        uris = meta.get("redirect_uris") or []
        if meta.get("client_id") != url or not uris or not all(isinstance(u, str) and redirect_ok(u) for u in uris):
            return None
        method = meta.get("token_endpoint_auth_method") or "none"
        c = {"client_id": url, "client_name": str(meta.get("client_name") or urlparse(url).hostname)[:120],
             "redirect_uris": uris[:10], "token_endpoint_auth_method": "none" if method == "none" else "unsupported",
             "secret_hash": ""}
        if c["token_endpoint_auth_method"] != "none":
            return None                               # metadata clients are public (PKCE)
        self._cimd[url] = (time.time() + 3600, c)
        return c

    def check_authorize(self, p: dict) -> dict:
        c = self.client(p["client_id"])
        if c is None:
            raise OAuthError("invalid_client", "Unknown app. Remove the connector and add it again.")
        if p["redirect_uri"] not in c["redirect_uris"]:
            raise OAuthError("invalid_request", "That redirect address isn't registered for this app.")
        if p["response_type"] != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
        if not p["code_challenge"] or p["code_challenge_method"] != "S256":
            raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required.")
        return c

    def issue_code(self, p: dict) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            data["codes"].append({"hash": self._hash(code), "client_id": p["client_id"], "redirect_uri": p["redirect_uri"],
                                  "challenge": p["code_challenge"], "scope": p["scope"] or "mcp",
                                  "resource": p["resource"], "expires": time.time() + CODE_TTL})
            self._save(data)
        return code

    def _client_auth(self, c: dict, secret: str) -> None:
        if c["token_endpoint_auth_method"] != "none" and not (
                secret and hmac.compare_digest(c.get("secret_hash", ""), self._hash(secret))):
            raise OAuthError("invalid_client", "Client authentication failed.", 401)

    def _tokens(self, data: dict, client_id: str, scope: str, resource: str, family: str = "") -> dict:
        access = f"{self.prefix}_oat_" + secrets.token_urlsafe(32)
        refresh = f"{self.prefix}_ort_" + secrets.token_urlsafe(32)
        now = time.time()
        c = self.client(client_id, data) or {}
        data["tokens"].append({"access": self._hash(access), "refresh": self._hash(refresh), "client_id": client_id,
                               "client_name": c.get("client_name", ""), "scope": scope, "resource": resource,
                               "family": family or secrets.token_hex(8), "created": now,
                               "access_expires": now + ACCESS_TTL, "refresh_expires": now + REFRESH_TTL})
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh,
                "scope": scope or "mcp"}

    def exchange_code(self, client_id: str, code: str, redirect_uri: str, verifier: str, secret: str = "") -> dict:
        with self._lock:
            data = self._load()
            c = self.client(client_id, data)
            if c is None:
                raise OAuthError("invalid_client", "Unknown client.", 401)
            self._client_auth(c, secret)
            digest = self._hash(code or "")
            grant = next((g for g in data["codes"] if hmac.compare_digest(g["hash"], digest)), None)
            if grant is None or grant["expires"] < time.time():
                raise OAuthError("invalid_grant", "The code is invalid or expired.")
            data["codes"].remove(grant)                       # single use
            if grant["client_id"] != client_id or grant["redirect_uri"] != redirect_uri:
                self._save(data)
                raise OAuthError("invalid_grant", "The code was issued to another client or redirect.")
            expected = base64.urlsafe_b64encode(hashlib.sha256((verifier or "").encode()).digest()).rstrip(b"=").decode()
            if not verifier or not hmac.compare_digest(expected, grant["challenge"]):
                self._save(data)
                raise OAuthError("invalid_grant", "PKCE verification failed.")
            out = self._tokens(data, client_id, grant["scope"], grant["resource"])
            self._save(data)
        return out

    def refresh(self, client_id: str, token: str, secret: str = "") -> dict:
        """Rotate: the old refresh token stops working."""
        with self._lock:
            data = self._load()
            c = self.client(client_id, data)
            if c is None:
                raise OAuthError("invalid_client", "Unknown client.", 401)
            self._client_auth(c, secret)
            digest = self._hash(token or "")
            row = next((t for t in data["tokens"] if hmac.compare_digest(t["refresh"], digest)), None)
            if row is None or row["client_id"] != client_id or row["refresh_expires"] < time.time():
                raise OAuthError("invalid_grant", "The refresh token is invalid or expired.")
            data["tokens"].remove(row)
            out = self._tokens(data, client_id, row["scope"], row["resource"], row["family"])
            self._save(data)
        return out

    def verify(self, raw: str) -> bool:
        """An access token from a connected app (API keys are checked by the app itself)."""
        if not raw or not raw.startswith(f"{self.prefix}_oat_"):
            return False
        digest = self._hash(raw)
        row = next((t for t in self._load()["tokens"] if hmac.compare_digest(t["access"], digest)), None)
        return bool(row) and row["access_expires"] > time.time()

    def revoke_token(self, raw: str) -> None:
        digest = self._hash(raw or "")
        with self._lock:
            data = self._load()
            row = next((t for t in data["tokens"] if hmac.compare_digest(t["access"], digest)
                        or hmac.compare_digest(t["refresh"], digest)), None)
            if row:
                data["tokens"] = [t for t in data["tokens"] if t["family"] != row["family"]]
                self._save(data)

    def connections(self) -> list[dict]:
        out: dict[str, dict] = {}
        for t in self._load()["tokens"]:
            row = out.setdefault(t["family"], {"id": t["family"], "client_name": t.get("client_name") or "MCP client",
                                               "created": t["created"]})
            row["created"] = min(row["created"], t["created"])
        return sorted(out.values(), key=lambda r: -r["created"])

    def challenge(self, headers) -> str:
        """The WWW-Authenticate value for a 401 on the MCP endpoint: it tells clients where to sign in."""
        return f'Bearer resource_metadata="{base_url(headers)}/.well-known/oauth-protected-resource{self.mcp_path}"'

    # ------------------------------------------------------------------ HTTP
    def routes(self) -> list[Route]:
        return [Route("/.well-known/oauth-protected-resource", self._resource),
                Route(f"/.well-known/oauth-protected-resource{self.mcp_path}", self._resource),
                Route("/.well-known/oauth-authorization-server", self._server),
                Route(f"/.well-known/oauth-authorization-server{self.mcp_path}", self._server),
                Route("/.well-known/openid-configuration", self._server),
                Route("/oauth/register", self._register, methods=["POST"]),
                Route("/oauth/authorize", self._authorize, methods=["GET", "POST"]),
                Route("/oauth/token", self._token, methods=["POST"]),
                Route("/oauth/revoke", self._revoke, methods=["POST"])]

    PUBLIC_PREFIXES = ("/.well-known/oauth-", "/.well-known/openid-configuration", "/oauth/")

    async def _resource(self, request: Request) -> JSONResponse:
        b = base_url(request.headers, request.url.scheme)
        return JSONResponse({"resource": f"{b}{self.mcp_path}", "authorization_servers": [b], "scopes_supported": ["mcp"],
                             "bearer_methods_supported": ["header"], "resource_name": self.name}, headers=OPEN)

    async def _server(self, request: Request) -> JSONResponse:
        b = base_url(request.headers, request.url.scheme)
        return JSONResponse({
            "issuer": b, "authorization_endpoint": f"{b}/oauth/authorize", "token_endpoint": f"{b}/oauth/token",
            "registration_endpoint": f"{b}/oauth/register", "revocation_endpoint": f"{b}/oauth/revoke",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": ["mcp"],
            "client_id_metadata_document_supported": True,
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"]},
            headers=OPEN)

    @staticmethod
    def _error(exc: OAuthError) -> JSONResponse:
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=exc.status,
                            headers=OPEN)

    async def _register(self, request: Request) -> JSONResponse:
        try:
            meta = await request.json()
        except ValueError:
            return self._error(OAuthError("invalid_client_metadata", "Send JSON"))
        try:
            return JSONResponse(self.register(meta if isinstance(meta, dict) else {}), status_code=201, headers=OPEN)
        except OAuthError as exc:
            return self._error(exc)

    def _page(self, c: dict, p: dict, error: str = "") -> HTMLResponse:
        fields = "".join(f'<input type="hidden" name="{k}" value="{escape(p.get(k) or "")}">' for k in PARAMS)
        err = f'<div class="err" role="alert">{escape(error)}</div>' if error else ""
        html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect to {escape(self.name)}</title><style>
:root{{--bg:#f4f5f7;--card:#fff;--text:#0f1419;--muted:#5b6670;--line:#dcdfe3;--go:#0f7b63}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b1014;--card:#12181d;--text:#e8ecef;--muted:#9aa6ae;--line:#2a343b;--go:#19b88a}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}}
.card{{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:var(--muted);margin:0 0 14px}}b{{color:var(--text)}}
input{{width:100%;box-sizing:border-box;height:42px;padding:0 12px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);font:inherit;margin:6px 0 14px}}
.row{{display:flex;gap:10px}}button{{flex:1;height:42px;border-radius:8px;font:600 14px system-ui;cursor:pointer;
border:1px solid var(--line);background:var(--card);color:var(--text)}}
button.go{{background:var(--go);border-color:var(--go);color:#fff}}.err{{color:#d92d20;font-size:13px;margin:-4px 0 12px}}
small{{display:block;color:var(--muted);margin-top:14px;font-size:12px}}</style></head><body>
<form class="card" method="post" action="/oauth/authorize">
<h1>Connect {escape(c["client_name"])} to {escape(self.name)}?</h1>
<p><b>{escape(c["client_name"])}</b> wants to use {escape(self.name)}'s tools through MCP, with the same access as its
key. It will send you back to <b>{escape(urlparse(p["redirect_uri"]).netloc)}</b>.</p>
{fields}{err}<label>{escape(self.password_label)}<input type="password" name="password" autocomplete="current-password"
required autofocus></label>
<div class="row"><button name="decision" value="deny">Deny</button><button class="go" name="decision" value="allow">Allow</button></div>
<small>Disconnect it any time by removing the connector in that app.</small></form></body></html>"""
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @staticmethod
    def _bad(message: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><title>Sign-in problem</title><p style='font:15px system-ui;padding:24px'>"
                            f"{escape(message)}</p>", status_code=400)

    def _too_many(self, ip: str) -> bool:
        now = time.time()
        self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < 900]
        return len(self._fails[ip]) >= 10

    async def _authorize(self, request: Request):
        src = await request.form() if request.method == "POST" else request.query_params
        p = {k: str(src.get(k, "") or "") for k in PARAMS}
        try:
            c = self.check_authorize(p)
        except OAuthError as exc:
            return self._bad(exc.description)
        if request.method == "GET":
            return self._page(c, p)
        back = {"state": p["state"]} if p["state"] else {}
        sep = "&" if "?" in p["redirect_uri"] else "?"
        if src.get("decision") != "allow":
            return RedirectResponse(p["redirect_uri"] + sep + urlencode({"error": "access_denied", **back}), 302)
        ip = request.client.host if request.client else "?"
        if self._too_many(ip):
            return self._page(c, p, "Too many tries. Wait 15 minutes.")
        if not self.check_password(str(src.get("password", ""))):
            self._fails.setdefault(ip, []).append(time.time())
            return self._page(c, p, "That's not right. Try again.")
        code = self.issue_code(p)
        iss = base_url(request.headers, request.url.scheme)
        return RedirectResponse(p["redirect_uri"] + sep + urlencode({"code": code, **back, "iss": iss}), 302)

    async def _token(self, request: Request) -> JSONResponse:
        form = await request.form()
        cid, secret = str(form.get("client_id", "")), str(form.get("client_secret", ""))
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                pass
        grant = str(form.get("grant_type", ""))
        try:
            if grant == "authorization_code":
                out = self.exchange_code(cid, str(form.get("code", "")), str(form.get("redirect_uri", "")),
                                         str(form.get("code_verifier", "")), secret)
            elif grant == "refresh_token":
                out = self.refresh(cid, str(form.get("refresh_token", "")), secret)
            else:
                raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
        except OAuthError as exc:
            return self._error(exc)
        return JSONResponse(out, headers={**OPEN, "Pragma": "no-cache"})

    async def _revoke(self, request: Request) -> JSONResponse:
        form = await request.form()
        self.revoke_token(str(form.get("token", "")))
        return JSONResponse({}, headers=OPEN)
