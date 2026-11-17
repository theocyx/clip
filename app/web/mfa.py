"""Connexion à l'interface : mot de passe + code à 6 chiffres (TOTP, Google Authenticator & co).

- Le secret TOTP est stocké dans data/mfa.json, la clé de signature des sessions dans data/session.key.
- Perte du téléphone : `docker compose exec web python -m app.web.mfa reset`, puis reconnexion
  (un nouveau QR code est proposé et toutes les sessions ouvertes sont déconnectées).
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import sys
import threading
import time
from pathlib import Path

import pyotp

from ..config import get_settings

ISSUER = "Clip"
SESSION_COOKIE = "clip_session"
SETUP_COOKIE = "clip_mfa_setup"
SETUP_TTL = 15 * 60
MAX_FAILURES, FAILURE_WINDOW = 10, 15 * 60  # au-delà : connexions bloquées 15 min

_lock = threading.Lock()
_failures: list[float] = []
_pending_secret: str | None = None  # secret proposé pendant l'activation, pas encore confirmé


def _mfa_file() -> Path:
    return get_settings().data_dir / "mfa.json"


def _key_file() -> Path:
    return get_settings().data_dir / "session.key"


def _session_key() -> bytes:
    f = _key_file()
    with _lock:
        if not f.is_file():
            f.write_bytes(secrets.token_bytes(32))
            f.chmod(0o600)
        return f.read_bytes()


def _load() -> dict:
    try:
        return json.loads(_mfa_file().read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    f = _mfa_file()
    f.write_text(json.dumps(data))
    f.chmod(0o600)


def configured() -> bool:
    return bool(_load().get("secret"))


def reset() -> None:
    """Supprime le secret TOTP et déconnecte toutes les sessions."""
    global _pending_secret
    _mfa_file().unlink(missing_ok=True)
    _key_file().unlink(missing_ok=True)
    _pending_secret = None


# ---------- Mot de passe + anti force brute ----------

def blocked() -> bool:
    now = time.time()
    with _lock:
        _failures[:] = [t for t in _failures if now - t < FAILURE_WINDOW]
        return len(_failures) >= MAX_FAILURES


def record_failure() -> None:
    with _lock:
        _failures.append(time.time())
    time.sleep(1)  # ralentit les essais en série


def check_password(user: str, password: str) -> bool:
    s = get_settings()
    return (secrets.compare_digest(user.encode(), s.admin_user.encode())
            & secrets.compare_digest(password.encode(), s.admin_password.encode()))


# ---------- Codes TOTP ----------

def _clean(code: str) -> str:
    return "".join(c for c in code if c.isdigit())


def verify_code(code: str) -> bool:
    """Vérifie un code du secret enregistré ; un même code ne sert qu'une fois."""
    data = _load()
    secret = data.get("secret")
    code = _clean(code)
    if not secret or len(code) != 6:
        return False
    totp = pyotp.TOTP(secret)
    now = time.time()
    for drift in (0, -1, 1):  # tolère ±30 s de décalage d'horloge
        counter = int(now // totp.interval) + drift
        if hmac.compare_digest(totp.generate_otp(counter), code):
            with _lock:
                data = _load()
                if counter <= data.get("last_counter", -1):
                    return False
                data["last_counter"] = counter
                _save(data)
            return True
    return False


def pending_secret() -> str:
    global _pending_secret
    with _lock:
        if not _pending_secret:
            _pending_secret = pyotp.random_base32()
        return _pending_secret


def confirm_setup(code: str) -> bool:
    global _pending_secret
    secret = _pending_secret
    if not secret or not pyotp.TOTP(secret).verify(_clean(code), valid_window=1):
        return False
    with _lock:
        _save({"secret": secret, "last_counter": int(time.time() // 30)})
        _pending_secret = None
    return True


def provisioning_uri(secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=get_settings().admin_user, issuer_name=ISSUER)


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# ---------- Cookies signés ----------

def _sign(purpose: str, exp: int) -> str:
    return hmac.new(_session_key(), f"{purpose}.{exp}".encode(), hashlib.sha256).hexdigest()


def make_token(purpose: str, ttl: int) -> str:
    exp = int(time.time()) + ttl
    return f"{exp}.{_sign(purpose, exp)}"


def valid_token(purpose: str, token: str | None) -> bool:
    try:
        exp_s, sig = (token or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(purpose, exp))


def session_ttl() -> int:
    return int(get_settings().session_days * 86400)


def main(argv: list[str]) -> int:
    if argv[1:] == ["reset"]:
        reset()
        print("MFA réinitialisée : reconnecte-toi pour scanner un nouveau QR code.")
        return 0
    print("Usage : python -m app.web.mfa reset")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
