"""Files between Iriis and Iriis HQ: images HQ returns are kept as her files; her files go to HQ by upload."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from iriis.storage import DB

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    import iriis.storage as st
    d = DB("", tmp_path / "f.db")
    monkeypatch.setattr(st, "get_db", lambda *a, **k: d)
    return d


def test_image_and_file_blocks_are_kept_as_her_files(sdb):
    from iriis.mcp_client.manager import keep_block
    note = keep_block(SimpleNamespace(type="image", data=PNG, mimeType="image/png"), "hq-get_file-2")
    assert note.startswith("hq-get_file-2.png ready: file 1")
    name, mime, data = sdb.execute("SELECT name, mime, data FROM files WHERE id = 1")[0]
    assert mime == "image/png" and base64.b64decode(data).startswith(b"\x89PNG")
    pdf = SimpleNamespace(type="resource", resource=SimpleNamespace(blob=base64.b64encode(b"%PDF-1.7").decode(),
                                                                   mimeType="application/pdf", uri="file:///x/deck.pdf"))
    assert keep_block(pdf, "hq").startswith("deck.pdf ready: file 2")
    txt = SimpleNamespace(type="resource", resource=SimpleNamespace(text="plain notes", uri="x"))
    assert keep_block(txt, "hq") == "plain notes"
    assert keep_block(SimpleNamespace(type="image", data=None), "hq") == "[image omitted]"


def test_send_to_hq_uploads_the_file(sdb, monkeypatch):
    from servers import studio as S
    monkeypatch.setattr(S, "get_db", lambda: sdb)
    fid = S.store("pilot.pdf", b"%PDF-1.7 pilot", "application/pdf")
    seen = {}

    def fake_post(url, timeout, headers, files):
        seen.update(url=url, auth=headers["Authorization"], name=files["file"][0])
        return SimpleNamespace(status_code=200, json=lambda: {"id": "a41f", "kind": "document", "pages": 12,
                                                              "library_doc": "d1"}, text="")
    monkeypatch.setattr(S.httpx, "post", fake_post)
    monkeypatch.delenv("IRIIS_HQ_KEY", raising=False)
    with pytest.raises(ValueError, match="isn't connected"):
        S.send_to_hq(fid)
    monkeypatch.setenv("IRIIS_HQ_KEY", "k")
    out = S.send_to_hq(fid)
    assert seen == {"url": "https://iriis.riftfi.com/api/v1/uploads", "auth": "Bearer k", "name": "pilot.pdf"}
    assert "upload id a41f (kind document, pages 12)" in out and "library" in out and 'attachment_ids=["a41f"]' in out
