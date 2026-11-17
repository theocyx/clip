"""Notifications Discord et/ou Telegram (clip publié, erreur)."""
from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


def configured() -> bool:
    s = get_settings()
    return bool(s.discord_webhook_url or (s.telegram_bot_token and s.telegram_chat_id))


def send(text: str) -> bool:
    """Envoie le message sur chaque service configuré. Ne lève jamais d'exception."""
    s = get_settings()
    ok = False
    try:
        with httpx.Client(timeout=15) as http:
            if s.discord_webhook_url:
                r = http.post(s.discord_webhook_url, json={"content": text[:2000]})
                ok |= r.is_success
                if not r.is_success:
                    log.warning("Notification Discord refusée : %s %s", r.status_code, r.text[:200])
            if s.telegram_bot_token and s.telegram_chat_id:
                r = http.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                              json={"chat_id": s.telegram_chat_id, "text": text[:4000],
                                    "disable_web_page_preview": True})
                ok |= r.is_success
                if not r.is_success:
                    log.warning("Notification Telegram refusée : %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Notification impossible : %s", e)
    return ok


def published(platform: str, title: str, remote_id: str = "") -> None:
    if not get_settings().notify_published:
        return
    name = {"youtube": "YouTube Shorts", "tiktok": "TikTok"}.get(platform, platform)
    link = f"\nhttps://youtube.com/shorts/{remote_id}" if platform == "youtube" and remote_id else ""
    send(f"✅ Clip publié sur {name} : {title}{link}")


def error(what: str, detail: str = "") -> None:
    if not get_settings().notify_errors:
        return
    send(f"❌ {what}" + (f"\n{detail[:500]}" if detail else ""))
