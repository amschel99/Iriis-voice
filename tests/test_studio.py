"""Cloud studio: charts, diagrams and documents stored in the database, delivered to Telegram."""
from __future__ import annotations

import shutil

import pytest

from servers import studio as S


@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    from iriis.storage import DB
    d = DB("", tmp_path / "s.sqlite3")
    monkeypatch.setattr(S, "get_db", lambda: d)
    return d


@pytest.mark.parametrize("kind", ["bar", "stacked_bar", "barh", "line", "area", "pie", "donut", "histogram"])
def test_every_chart_kind_renders(kind):
    png = S.render_chart(kind, "t", ["a", "b", "c"], {"x": [1, 2, 3], "y": [2, 1, 4]})
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000


def test_bad_chart_is_explained():
    with pytest.raises(ValueError, match="Unknown chart kind"):
        S.render_chart("radar3d", "t", ["a"], {"x": [1]})


def test_chart_then_document_with_it_inside(sdb):
    out = S.make_chart("bar", "Rentals", {"Cool": [120, 150]}, ["Sep", "Oct"])
    assert "file 1" in out
    out = S.make_document("pdf", "Update", "# Update\n![Rentals](file:1)\nGood month.")
    assert "PDF ready: file 2" in out
    name, mime, data = S.load(2)
    assert name == "Update.pdf" and data[:5] == b"%PDF-"
    from pypdf import PdfReader
    import io
    assert len(PdfReader(io.BytesIO(data)).pages[0].images) == 1
    assert "file 2: Update.pdf" in S.list_files()


@pytest.mark.skipif(not shutil.which("dot") and not __import__("os").path.isdir(r"C:\Program Files\Graphviz\bin"),
                    reason="Graphviz not installed")
def test_diagram_and_dot_errors(sdb):
    assert "Diagram ready" in S.make_diagram("A -> B -> C")
    with pytest.raises(ValueError, match="mistake"):
        S.make_diagram("digraph { A -> ; }")


def test_send_file_needs_telegram(sdb, monkeypatch):
    S.make_chart("pie", "Split", {"s": [1, 2]}, ["a", "b"])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Telegram isn't set up"):
        S.send_file(1)
    sent = {}

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post(url, **kw):
        sent.update(url=url, files=kw["files"], data=kw["data"])
        return R()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(S.httpx, "post", fake_post)
    assert "Sent" in S.send_file(1, caption="hi")
    assert sent["url"].endswith("/sendPhoto") and sent["data"]["chat_id"] == "42" and "photo" in sent["files"]
