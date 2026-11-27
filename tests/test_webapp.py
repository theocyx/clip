"""Appli sur l'écran d'accueil de l'iPhone (manifest, icônes, service worker, mise en page mobile)."""
import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from app.web.main import app, auth

client = TestClient(app)


def _png_size(r):
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    return Image.open(io.BytesIO(r.content)).size


def test_manifest_public_and_standalone():
    r = client.get("/manifest.webmanifest", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["display"] == "standalone" and m["start_url"] == "/" and m["short_name"] == "Clip"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for i in m["icons"]:
        w, h = _png_size(client.get(i["src"], follow_redirects=False))
        assert f"{w}x{h}" == i["sizes"]


def test_apple_touch_icon_public():
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        assert _png_size(client.get(path, follow_redirects=False)) == (180, 180)
    assert client.get("/favicon.ico", follow_redirects=False).status_code == 200


def test_service_worker_and_offline_page():
    r = client.get("/sw.js", follow_redirects=False)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "/offline" in r.text and "r.mode !== 'navigate'" in r.text  # ne touche qu'aux pages, jamais aux envois
    r = client.get("/offline", follow_redirects=False)
    assert r.status_code == 200 and "Réessayer" in r.text


def test_static_files_do_not_open_the_admin():
    assert client.get("/static/../main.py", follow_redirects=False).status_code == 404
    assert client.get("/clips", follow_redirects=False).status_code == 303


def test_login_page_is_installable():
    html = client.get("/login").text
    for tag in ('rel="manifest"', 'rel="apple-touch-icon"', 'name="apple-mobile-web-app-capable" content="yes"',
                'apple-mobile-web-app-status-bar-style', 'viewport-fit=cover'):
        assert tag in html, tag
    assert 'class="tabs"' not in html  # pas d'onglets avant la connexion


def test_tab_bar_marks_current_page():
    app.dependency_overrides[auth] = lambda: None
    try:
        html = client.get("/sources").text
    finally:
        app.dependency_overrides.pop(auth, None)
    assert 'class="tabs"' in html
    assert '<a href="/sources" class="on" aria-current="page">' in html
    assert '<a href="/" class="on"' not in html
    assert 'class="list"' in html
