"""Journaux gardés dans data/logs (worker.log, web.log), en plus de la sortie Docker.

Une mise à jour (docker compose up --build) recrée les conteneurs et efface leurs journaux : sans ce fichier,
impossible de retrouver une erreur d'avant le redéploiement. 5 fichiers de 10 Mo au plus par rôle."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import get_settings

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup(role: str) -> None:
    """Ajoute le fichier data/logs/<role>.log aux journaux (une seule fois, jamais bloquant)."""
    root = logging.getLogger()
    name = f"{role}.log"
    if any(getattr(h, "baseFilename", "").endswith(name) for h in root.handlers):
        return
    try:
        d = get_settings().data_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(d / name, maxBytes=10_000_000, backupCount=4, encoding="utf-8")
    except OSError as e:
        logging.getLogger(__name__).warning("Journal %s impossible : %s", name, e)
        return
    h.setFormatter(logging.Formatter(FORMAT))
    h.setLevel(logging.INFO)
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
