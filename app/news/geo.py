"""Lieu de l'actu pour le survol « vu d'en haut » : position (Nominatim / OpenStreetMap) et tuiles satellite
libres (par défaut Sentinel-2 cloudless 2016 d'EOX, licence CC BY 4.0, créditée), gardées en cache sur le disque."""
from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass, field
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


@dataclass
class Place:
    lon: float
    lat: float
    name: str
    kind: str = ""
    sub: str = ""  # pays (ou région) affiché sous le nom
    rings: list = field(default_factory=list)  # contours du lieu [(lon, lat), …] pour le surligner


def _rings(geojson: dict | None, lon: float, lat: float, max_points: int = 1500) -> list[list[tuple[float, float]]]:
    """Contours extérieurs proches du lieu (on ignore p. ex. l'outre-mer quand on montre la métropole)."""
    if not geojson:
        return []
    kind, coords = geojson.get("type"), geojson.get("coordinates") or []
    polys = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    out = []
    for poly in polys:
        if not poly or len(poly[0]) < 3:
            continue
        ring = [(float(x), float(y)) for x, y, *_ in poly[0]]
        xs, ys = [x for x, _ in ring], [y for _, y in ring]
        if min(xs) - 8 <= lon <= max(xs) + 8 and min(ys) - 8 <= lat <= max(ys) + 8:
            step = max(1, len(ring) // max_points)
            out.append(ring[::step])
    return out


def geocode(place: str) -> Place | None:
    """Lieu (position, nom court, type, pays, contours), ou None. Une requête par seconde maximum (règle Nominatim)."""
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
                      params={"q": place, "format": "jsonv2", "limit": 1, "accept-language": "fr",
                              "polygon_geojson": 1, "polygon_threshold": 0.005})
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        log.info("Lieu introuvable (%s) : %s", place, e)
        return None
    if not rows:
        return None
    row = rows[0]
    lon, lat = float(row["lon"]), float(row["lat"])
    name = (row.get("name") or place).strip()
    parts = [x.strip() for x in str(row.get("display_name") or "").split(",") if x.strip()]
    sub = parts[-1] if parts and parts[-1].lower() != name.lower() else ""
    return Place(lon, lat, name, row.get("addresstype") or row.get("type") or "", sub,
                 _rings(row.get("geojson"), lon, lat))


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
