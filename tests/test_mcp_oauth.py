"""MCP sign-in, Claude's recommended way: the client_id is a URL that publishes Claude's details (CIMD)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import urllib.request

import pytest

from iriis.server.mcp_oauth import MCPOAuth, OAuthError

CID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
DOC = {"client_id": CID, "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
       "token_endpoint_auth_method": "none"}


@pytest.fixture()
def oa(monkeypatch):
    box = {}
    o = MCPOAuth("Test", "t", lambda: box.get("v"), lambda v: box.__setitem__("v", v), lambda pw: pw == "pw", "pw")
    fetched = []

    def fake_open(req, timeout=0):
        fetched.append(req.full_url)
        return io.BytesIO(json.dumps(DOC).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("160.79.104.10", 443))])
    return o, fetched


def test_claude_by_its_published_identity(oa):
    o, fetched = oa
    p = {"client_id": CID, "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "response_type": "code",
         "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(b"v" * 50).digest()).rstrip(b"=").decode(),
         "code_challenge_method": "S256", "state": "", "scope": "", "resource": ""}
    assert o.check_authorize(p)["client_name"] == "Claude"
    code = o.issue_code(p)
    tok = o.exchange_code(CID, code, p["redirect_uri"], "v" * 50)
    assert o.verify(tok["access_token"])
    assert o.refresh(CID, tok["refresh_token"])["access_token"]
    assert fetched == [CID]                                   # fetched once, then cached


def test_bad_metadata_and_private_hosts_are_refused(oa, monkeypatch):
    o, _ = oa
    DOC_BAD = {**DOC, "client_id": "https://evil.example/other"}
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: io.BytesIO(json.dumps(DOC_BAD).encode()))
    assert o.client("https://evil.example/claude") is None                 # client_id must match its URL
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("10.0.0.5", 443))])
    assert o.client("https://internal.example/x") is None                  # never fetch inside the network
    assert o.client("http://claude.ai/x") is None                          # https only
    with pytest.raises(OAuthError):
        o.check_authorize({"client_id": "https://internal.example/x", "redirect_uri": "", "response_type": "code",
                           "code_challenge": "c", "code_challenge_method": "S256"})
