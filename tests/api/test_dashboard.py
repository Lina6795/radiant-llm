"""M9 dashboard static mount + catch-all precedence: API 404s must stay
structured JSON and never be swallowed by the React catch-all."""

from __future__ import annotations


def test_dashboard_pages_served(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "RADIANT-Control" in resp.text
    assert "text/html" in resp.headers["content-type"]

    resp = client.get("/dashboard/app.js")
    assert resp.status_code == 200
    assert "EventSource" in resp.text

    resp = client.get("/dashboard/style.css")
    assert resp.status_code == 200

    # /dashboard (no trailing slash) redirects into the mount
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code in (301, 302, 307)


def test_catchall_does_not_swallow_api_404(client):
    resp = client.get("/runs/definitely-not-a-run")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert "not found" in resp.json()["detail"]

    resp = client.get("/evidence/ev-does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")

    resp = client.get("/benchmarks/bm-nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")

    resp = client.get("/documents/ingest/ing-nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_react_frontend_still_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
