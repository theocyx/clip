from fastapi.testclient import TestClient

from app.config import get_settings
from app.web.main import app

client = TestClient(app)


def test_public_pages_without_password():
    assert client.get("/conditions").status_code == 200
    assert client.get("/confidentialite").status_code == 200


def test_admin_needs_login():
    r = client.get("/clips", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/clips"
    assert client.post("/sources", data={"url": "x"}).status_code == 401


def test_verify_file():
    d = get_settings().data_dir / "verify"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tiktokABC123.txt").write_text("tiktok-developers-site-verification=ABC")
    r = client.get("/tiktokABC123.txt")
    assert r.status_code == 200 and "ABC" in r.text
    assert client.get("/absent.txt").status_code == 404


def test_suggested_channels_catalog():
    from app.pipeline import sources
    from app.web.main import suggested_channels

    chans = suggested_channels()
    assert len(chans) >= 30
    urls = [c["url"] for c in chans]
    assert len(urls) == len(set(urls))
    for c in chans:
        assert sources.detect_platform(c["url"]) == c["platform"]
        assert sources.normalize_channel_url(c["url"]) == c["url"]


def test_sources_page_lists_suggestions():
    from app.web.main import auth

    app.dependency_overrides[auth] = lambda: None
    try:
        r = client.get("/sources")
    finally:
        app.dependency_overrides.pop(auth, None)
    assert r.status_code == 200 and "twitch.tv/zerator" in r.text


def test_keyword_filter():
    from app.pipeline.sources import matches_keywords

    assert matches_keywords("n'importe quoi", "")
    assert matches_keywords("ON DÉCOUVRE GTA 6 !!", "GTA")
    assert matches_keywords("gta vi première soirée", "gta, gta vi")
    assert not matches_keywords("Minecraft hardcore", "GTA")
    assert not matches_keywords("agtar", "GTA")
