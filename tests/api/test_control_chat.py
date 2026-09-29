"""S11-E governed-chain chat frontend: static mount, asset wiring, dashboard
entry point, and XSS-safety invariants for the vanilla-JS chat page."""

from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "control-chat-static"
_DASHBOARD_HTML = Path(__file__).resolve().parents[2] / "app" / "dashboard-static" / "index.html"


def test_control_chat_pages_served(client):
    # /control-chat (no trailing slash) redirects into the mount
    resp = client.get("/control-chat", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"].endswith("/control-chat/")

    # /control must redirect too -- otherwise it falls through to the
    # legacy frontend catch-all and shows the old English UI
    resp = client.get("/control", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"].endswith("/control-chat/")

    resp = client.get("/control-chat/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

    resp = client.get("/control-chat/app.js")
    assert resp.status_code == 200
    assert "EventSource" in resp.text

    resp = client.get("/control-chat/style.css")
    assert resp.status_code == 200


def test_index_references_assets():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "app.js" in html
    assert "style.css" in html


def test_app_js_uses_governed_api():
    js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "/runs" in js
    assert "/events" in js
    assert "/evidence/" in js


def test_app_js_has_no_legacy_fallback():
    js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "/stream-query" not in js
    assert "convchain" not in js.lower()


def test_no_innerhtml_anywhere():
    # Server-supplied text must only ever reach the DOM via textContent /
    # createTextNode. A payload like "<script>alert(1)</script>" must stay
    # inert text, so innerHTML must not appear in the chat frontend at all
    # (not even in comments, to keep the invariant greppable).
    for name in ("app.js", "index.html"):
        text = (_STATIC_DIR / name).read_text(encoding="utf-8")
        assert "innerHTML" not in text, f"innerHTML found in {name}"


def test_index_has_no_inline_event_handlers():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE), (
        "inline event handler attribute found in index.html"
    )


def test_dashboard_links_control_chat():
    html = _DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "/control-chat/" in html
