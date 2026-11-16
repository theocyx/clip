import json

from .. import db
from ..models import Credential, now


def load(platform: str) -> dict | None:
    with db.session() as s:
        c = s.get(Credential, platform)
        return json.loads(c.data) if c else None


def account(platform: str) -> str:
    with db.session() as s:
        c = s.get(Credential, platform)
        return c.account if c else ""


def save(platform: str, data: dict, account: str | None = None) -> None:
    with db.session() as s:
        c = s.get(Credential, platform) or Credential(platform=platform, data="{}")
        c.data = json.dumps(data)
        if account is not None:
            c.account = account
        c.updated_at = now()
        s.add(c)
        s.commit()


def delete(platform: str) -> None:
    with db.session() as s:
        c = s.get(Credential, platform)
        if c:
            s.delete(c)
            s.commit()
