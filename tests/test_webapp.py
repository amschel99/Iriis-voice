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
