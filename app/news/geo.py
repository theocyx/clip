"""Lieu de l'actu pour le survol « vu d'en haut » : position (Nominatim / OpenStreetMap) et tuiles satellite
libres (par défaut Sentinel-2 cloudless 2016 d'EOX, licence CC BY 4.0, créditée), gardées en cache sur le disque."""
from __future__ import annotations

import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np

from ..config import get_settings

log = logging.getLogger(__name__)
UA = "ClipNews/1.0 (self-hosted video tool)"
ATTRIBUTION = "Image satellite : Sentinel-2 cloudless 2016 by EOX (CC BY 4.0)"
CREDIT = ("Carte : Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH (Contains modified Copernicus "
          "Sentinel data 2016), CC BY 4.0 ; lieu : © OpenStreetMap contributors")
_last_geocode = 0.0
_lock = threading.Lock()


def geocode(place: str) -> tuple[float, float, str, str] | None:
    """(longitude, latitude, nom court, type de lieu) d'un lieu, ou None. Une requête par seconde maximum (règle Nominatim)."""
    global _last_geocode
    if not place or not place.strip():
        return None
    with _lock:
        wait = 1.1 - (time.monotonic() - _last_geocode)
        if wait > 0:
            time.sleep(wait)
        _last_geocode = time.monotonic()
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=20,
                      params={"q": place, "format": "jsonv2", "limit": 1, "accept-language": "fr"})
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        log.info("Lieu introuvable (%s) : %s", place, e)
        return None
    if not rows:
        return None
    row = rows[0]
    name = (row.get("name") or place).strip()
    return float(row["lon"]), float(row["lat"]), name, row.get("addresstype") or row.get("type") or ""


def zoom_for(place_type: str) -> float:
    """Zoom final selon le type de lieu : un pays se voit de plus haut qu'une rue."""
    t = (place_type or "").lower()
    if t in ("country", "continent", "sea", "ocean"):
        return 6.0
    if t in ("state", "region", "province", "county"):
        return 8.5
    return 12.5


class Tiles:
    def __init__(self, url: str | None = None, cache: Path | None = None):
        s = get_settings()
        self.url = url or s.news_map_tiles
        self.cache = cache or (s.data_dir / "tiles")
        self.client = httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True)

    def path(self, z: int, x: int, y: int) -> Path:
        return self.cache / str(z) / str(x) / f"{y}.jpg"

    def get(self, z: int, x: int, y: int) -> np.ndarray | None:
        from PIL import Image

        p = self.path(z, x, y)
        try:
            if not p.exists():
                r = self.client.get(self.url.format(z=z, x=x, y=y))
                if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
                    return None
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(r.content)
            return np.asarray(Image.open(io.BytesIO(p.read_bytes())).convert("RGB").resize((256, 256)))
        except Exception as e:
            log.debug("Tuile %s/%s/%s : %s", z, x, y, e)
            return None

    def prefetch(self, keys) -> int:
        """Télécharge en parallèle les tuiles pas encore en cache. Renvoie le nombre de tuiles manquantes."""
        todo = [k for k in keys if not self.path(*k).exists()]
        with ThreadPoolExecutor(6) as pool:
            res = list(pool.map(lambda k: self.get(*k) is not None, todo))
        return res.count(False)
