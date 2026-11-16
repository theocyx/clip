from fastapi.testclient import TestClient

from app.config import get_settings
from app.web.main import app

client = TestClient(app)


def test_public_pages_without_password():
    assert client.get("/conditions").status_code == 200
    assert client.get("/confidentialite").status_code == 200


def test_admin_needs_password():
    assert client.get("/").status_code == 401


def test_verify_file():
    d = get_settings().data_dir / "verify"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tiktokABC123.txt").write_text("tiktok-developers-site-verification=ABC")
    r = client.get("/tiktokABC123.txt")
    assert r.status_code == 200 and "ABC" in r.text
    assert client.get("/absent.txt").status_code == 404
