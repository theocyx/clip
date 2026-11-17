import time

import pyotp
from fastapi.testclient import TestClient

from app.config import get_settings
from app.web import mfa
from app.web.main import app


def _client():
    return TestClient(app, base_url="https://testserver")


def test_mfa_setup_then_login(monkeypatch):
    monkeypatch.setattr(mfa.time, "sleep", lambda s: None)
    mfa.reset()
    mfa._failures.clear()
    s = get_settings()
    c = _client()
    # 1re connexion : mot de passe OK -> page d'activation avec QR code
    r = c.post("/login", data={"username": s.admin_user, "password": s.admin_password, "next": "/clips"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/mfa/setup")
    page = c.get(r.headers["location"])
    assert "<svg" in page.text
    secret = mfa.pending_secret()
    assert c.post("/mfa/setup", data={"code": "000000"}).status_code == 400
    r = c.post("/mfa/setup", data={"code": pyotp.TOTP(secret).now(), "next": "/clips"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clips"
    assert c.get("/clips").status_code == 200
    assert mfa.configured()

    # Nouvelle connexion : il faut le code, et un code déjà utilisé est refusé
    c2 = _client()
    data = {"username": s.admin_user, "password": s.admin_password}
    assert c2.post("/login", data=data).status_code == 401
    code = pyotp.TOTP(secret).at(time.time() + 30)
    r = c2.post("/login", data={**data, "code": code}, follow_redirects=False)
    assert r.status_code == 303 and c2.get("/").status_code == 200
    assert _client().post("/login", data={**data, "code": code}).status_code == 401
    # Mauvais mot de passe + bon code : refusé
    assert _client().post("/login", data={**data, "password": "x", "code": pyotp.TOTP(secret).now()}).status_code == 401
    # Setup impossible une fois activée
    assert c2.get("/mfa/setup", follow_redirects=False).headers["location"] == "/login"
    # Déconnexion
    c2.post("/logout")
    assert c2.get("/", follow_redirects=False).status_code == 303
    mfa.reset()
    mfa._failures.clear()


def test_bruteforce_blocked(monkeypatch):
    monkeypatch.setattr(mfa.time, "sleep", lambda s: None)
    mfa._failures.clear()
    c = _client()
    for _ in range(mfa.MAX_FAILURES):
        c.post("/login", data={"username": "a", "password": "b"})
    s = get_settings()
    r = c.post("/login", data={"username": s.admin_user, "password": s.admin_password})
    assert r.status_code == 429
    mfa._failures.clear()


def test_forged_cookie_rejected():
    c = _client()
    c.cookies.set(mfa.SESSION_COOKIE, f"{int(time.time()) + 999}.deadbeef")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_safe_next():
    from app.web.main import _safe_next
    assert _safe_next("//evil.com") == "/" and _safe_next("https://evil.com") == "/" and _safe_next("/clips") == "/clips"
