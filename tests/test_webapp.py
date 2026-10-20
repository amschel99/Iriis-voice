"""The Iriis app (/app): login, history, tasks, push notifications, files served to the app."""
from __future__ import annotations

import json
import time

from tests.test_server import AUTH, make_client


def test_login_cookie_opens_the_app_and_api(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/app"
        assert client.get("/app", follow_redirects=False).headers["location"] == "/app/login"
        assert "Iriis key" in client.get("/app/login").text
        assert client.post("/app/login", data={"key": "wrong"}).status_code == 401
        r = client.post("/app/login", data={"key": "test-key"}, follow_redirects=False)
        assert r.status_code == 303 and "iriis_key" in r.cookies
        assert "<title>Iriis</title>" in client.get("/app").text            # cookie now opens the app
        assert client.get("/v1/status").status_code == 200                 # ...and the API it uses
        for f in ("/app/sw.js", "/app/manifest.webmanifest", "/app/icon.svg", "/app/icon-192.png"):
            assert client.get(f).status_code == 200, f
        assert client.get("/app/logout", follow_redirects=False).status_code == 303


def test_history_after_a_chat(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, ["Hello there."], tmp_path)
    with client:
        client.post("/v1/chat", headers=AUTH, json={"message": "hi", "session_id": "owner", "stream": False})
        h = client.get("/v1/history", headers=AUTH).json()
        assert [m["role"] for m in h] == ["user", "assistant"] and h[1]["content"].startswith("Hello")
        assert all(isinstance(m["ts"], float) for m in h)
        assert client.get("/v1/history").status_code == 401


def test_history_ids_and_permanent_delete_refresh_her_context(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, ["Hello there.", "Second answer."], tmp_path)
    with client:
        client.post("/v1/chat", headers=AUTH, json={"message": "hi", "session_id": "owner", "stream": False})
        client.post("/v1/chat", headers=AUTH, json={"message": "secret plan", "session_id": "owner", "stream": False})
        h = client.get("/v1/history", headers=AUTH).json()
        assert len(h) == 4 and all(isinstance(m["id"], int) for m in h)
        agent = client.app.state.core.sessions.sessions["owner"].agent
        assert any("secret plan" in m["content"] for m in agent.history.messages)
        ids = [m["id"] for m in h if m["content"] in ("secret plan",) or m["content"].startswith("Second")]
        assert client.post("/v1/history/delete", json={"ids": ids}).status_code == 401
        r = client.post("/v1/history/delete", headers=AUTH, json={"ids": ids, "session": "owner"}).json()
        assert r["deleted"] == 2
        h2 = client.get("/v1/history", headers=AUTH).json()
        assert [m["content"] for m in h2][0] == "hi" and len(h2) == 2
        assert not any("secret plan" in m["content"] for m in agent.history.messages)   # she forgot it too
        # ids of another session are never touched
        assert client.post("/v1/history/delete", headers=AUTH, json={"ids": [h2[0]["id"]], "session": "other"}).json()["deleted"] == 0


def test_upload_meta_and_media_range(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        data = bytes(range(256)) * 40
        r = client.post("/v1/upload", headers=AUTH, files={"file": ("clip.mp4", data, "application/octet-stream")})
        j = r.json()
        assert r.status_code == 200 and j["mime"] == "video/mp4" and j["size"] == len(data) and j["name"] == "clip.mp4"
        assert client.post("/v1/upload", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 401
        meta = client.get(f"/v1/files/{j['id']}/meta", headers=AUTH).json()
        assert meta["name"] == "clip.mp4" and meta["size"] == len(data)
        assert client.get("/v1/files/9999/meta", headers=AUTH).status_code == 404
        full = client.get(f"/v1/media/{j['id']}", headers=AUTH)
        assert full.status_code == 200 and full.content == data and full.headers["accept-ranges"] == "bytes"
        part = client.get(f"/v1/media/{j['id']}", headers={**AUTH, "Range": "bytes=100-199"})
        assert part.status_code == 206 and part.content == data[100:200]
        assert part.headers["content-range"] == f"bytes 100-199/{len(data)}"
        tail = client.get(f"/v1/media/{j['id']}", headers={**AUTH, "Range": "bytes=-10"})
        assert tail.status_code == 206 and tail.content == data[-10:]
        assert client.get(f"/v1/media/{j['id']}", headers={**AUTH, "Range": f"bytes={len(data)}-"}).status_code == 416
        assert client.get(f"/v1/files/{j['id']}", headers=AUTH).content == data      # the studio route still works


def test_upload_size_limit(monkeypatch, tmp_path):
    import iriis.server.webapp as w
    monkeypatch.setattr(w, "MAX_UPLOAD", 1000)
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        assert client.post("/v1/upload", headers=AUTH, files={"file": ("big.bin", b"x" * 2000)}).status_code == 413


def test_linkpreview_refuses_private_addresses(monkeypatch, tmp_path):
    import iriis.server.webapp as w
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        for url in ("http://127.0.0.1:8790/v1/status", "http://169.254.169.254/latest/meta-data/", "http://localhost/",
                    "http://10.0.0.1/", "file:///etc/passwd", "ftp://example.com/", "http://[::1]/"):
            j = client.get("/v1/linkpreview", headers=AUTH, params={"url": url}).json()
            assert j["title"] == "" and j["image"] == "", url
        assert client.get("/v1/linkpreview", params={"url": "https://example.com"}).status_code == 401
    assert w._safe_url("https://93.184.216.34/") and not w._safe_url("http://192.168.1.4/")


def test_linkpreview_parses_open_graph():
    import iriis.server.webapp as w
    html = ('<html><head><title> Plain title </title><meta property="og:title" content="OG Title">'
            '<meta name="description" content="A page about things."><meta property="og:image" content="/img/card.png">'
            '<meta property="og:site_name" content="Example"></head></html>')
    p = w.parse_preview("https://www.example.com/a/b", html)
    assert p == {"url": "https://www.example.com/a/b", "title": "OG Title", "description": "A page about things.",
                 "image": "https://www.example.com/img/card.png", "site": "Example"}
    assert w.parse_preview("https://www.example.com/", "<title>Only</title>")["site"] == "example.com"


def test_calls_ring_answer_decline_cancel(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        db = client.app.state.core.db
        now = time.time()
        ring = db.insert("INSERT INTO calls (at, reason, opening, status, source, created) VALUES (?, ?, ?, ?, ?, ?)",
                         (now, "Wake up", "Good morning!", "ringing", "user", now))
        later = db.insert("INSERT INTO calls (at, reason, status, source, created) VALUES (?, ?, ?, ?, ?)",
                          (now + 3600, "Call mum", "scheduled", "iriis", now))
        assert [c["id"] for c in client.get("/v1/calls?status=ringing", headers=AUTH).json()] == [ring]
        assert [c["id"] for c in client.get("/v1/calls/upcoming", headers=AUTH).json()] == [later]
        a = client.post(f"/v1/calls/{ring}/answer", headers=AUTH).json()
        assert a == {"id": ring, "reason": "Wake up", "opening": "Good morning!"}
        assert client.post(f"/v1/calls/{ring}/answer", headers=AUTH).status_code == 404     # only once
        assert client.post(f"/v1/calls/{later}/cancel", headers=AUTH).json()["ok"]
        assert client.get("/v1/calls/upcoming", headers=AUTH).json() == []
        assert client.post(f"/v1/calls/{later}/decline", headers=AUTH).status_code == 404


def test_push_all_merges_call_data(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        db = client.app.state.core.db
        client.post("/v1/push/subscribe", headers=AUTH, json={"endpoint": "https://push.example/z", "keys": {"p256dh": "x", "auth": "y"}})
        sent = []
        import asyncio

        import iriis.server.webapp as w
        monkeypatch.setattr(w, "_send_one", lambda sub, payload, priv: (sent.append(json.loads(payload)), 201)[1])
        assert asyncio.run(w.push_all(db, "Iriis is calling", "Wake up", url="/app?call=3",
                                      data={"kind": "call", "call_id": 3})) == 1
        assert sent[0] == {"kind": "call", "call_id": 3, "title": "Iriis is calling", "body": "Wake up", "url": "/app?call=3"}


def test_tasks_list_cancel_and_run(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        db = client.app.state.core.db
        db.execute("INSERT INTO tasks (title, goal, repeat, status, next_run, runs, created) VALUES "
                   "('Brief', 'brief me', 'weekdays 07:00', 'scheduled', ?, 0, ?)", (time.time() + 3600, time.time()))
        items = client.get("/v1/tasks", headers=AUTH).json()
        assert items[0]["title"] == "Brief" and items[0]["status"] == "scheduled"
        tid = items[0]["id"]
        assert client.post(f"/v1/tasks/{tid}/run", headers=AUTH).json()["ok"]
        assert client.post(f"/v1/tasks/{tid}/cancel", headers=AUTH).json()["ok"]
        assert client.get("/v1/tasks", headers=AUTH).json()[0]["status"] == "cancelled"
        assert client.post(f"/v1/tasks/{tid}/cancel", headers=AUTH).status_code == 404


def test_push_key_is_made_once_and_subscriptions_are_kept(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        k1 = client.get("/v1/push/key", headers=AUTH).json()["key"]
        k2 = client.get("/v1/push/key", headers=AUTH).json()["key"]
        assert k1 == k2 and len(k1) > 80                                   # same key every time (stored in the DB)
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
        assert client.post("/v1/push/subscribe", headers=AUTH, json=sub).json()["ok"]
        assert client.post("/v1/push/subscribe", headers=AUTH, json={"endpoint": "http://bad"}).status_code == 400
        db = client.app.state.core.db
        assert db.execute("SELECT COUNT(*) FROM push_subs")[0][0] == 1
        sent = []
        import iriis.server.webapp as w
        monkeypatch.setattr(w, "_send_one", lambda sub, payload, priv: (sent.append(json.loads(payload)), 410)[1])
        import asyncio
        asyncio.run(w.push_all(db, "Iriis", "Your task is done."))
        assert sent[0]["body"] == "Your task is done."
        assert db.execute("SELECT COUNT(*) FROM push_subs")[0][0] == 0       # a gone subscription (410) is removed


def test_camera_off_until_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("VISION_MCP_URL", raising=False)
    monkeypatch.delenv("VISION_ACCESS_KEY", raising=False)
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        st = client.get("/v1/camera/status", headers=AUTH).json()
        assert st["connected"] is False and "VISION_MCP_URL" in st["why"]
        assert client.get("/v1/bench/status", headers=AUTH).status_code == 503
        assert client.get("/app/camera.js").status_code == 200
        with client.websocket_connect("/v1/camera?token=test-key") as ws:
            m = json.loads(ws.receive_text())
            assert m["type"] == "error" and m["fatal"] and "VISION_MCP_URL" in m["text"]


def test_bench_proxy_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_MCP_URL", "https://bench.example/mcp")
    monkeypatch.setenv("VISION_ACCESS_KEY", "vk")
    seen = []

    class R:
        status_code, content, headers = 200, b"{}", {"content-type": "application/json"}

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, params=None, content=None, headers=None):
            seen.append((method, url, headers.get("Authorization")))
            return R()

    import iriis.server.camera as cam
    monkeypatch.setattr(cam.httpx, "AsyncClient", C)
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        client.get("/v1/bench/media/12", headers=AUTH)
        client.post("/v1/bench/media/12/analyze", headers=AUTH, json={})
        client.get("/v1/bench/procedures", headers=AUTH)
    assert [u for _, u, _ in seen] == ["https://bench.example/media/12", "https://bench.example/api/media/12/analyze",
                                       "https://bench.example/api/procedures"]
    assert all(a == "Bearer vk" for _, _, a in seen)


def test_bad_recording_is_explained_and_logged(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, [], tmp_path)
    with client:
        r = client.post("/v1/stt", headers=AUTH, files={"file": ("voice.webm", b"\x1aE\xdf\xa3" + b"\x00" * 600, "audio/webm")})
        assert r.status_code == 422 and "couldn't read that recording" in r.json()["detail"]
        assert client.post("/v1/stt", headers=AUTH, files={"file": ("v.webm", b"x", "audio/webm")}).status_code == 422
        errs = client.get("/v1/debug/errors", headers=AUTH).json()
        assert errs[0]["where"] == "stt" and errs[1]["where"] == "stt decode" and errs[1]["info"]["type"] == "audio/webm"
