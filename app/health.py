"""Santé du serveur : disque, température du CPU, mémoire, services (worker, interface web, IA).

Le worker surveille la machine, l'interface web et Ollama ; l'interface web surveille le worker
(chacun peut prévenir si l'autre est tombé). Une alerte est envoyée quand un problème apparaît,
rappelée toutes les HEALTH_REPEAT_HOURS tant qu'il dure, puis un message quand c'est réglé."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

import httpx

from . import notify
from .config import get_settings

log = logging.getLogger(__name__)

SYS = Path("/sys/class")
PROC_MEMINFO = Path("/proc/meminfo")
CPU_SENSORS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz")


def _dir() -> Path:
    d = get_settings().data_dir / "health"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- Mesures ----------

def disk() -> dict:
    u = shutil.disk_usage(get_settings().data_dir)
    return {"pct": round(100 * u.used / u.total, 1) if u.total else 0.0,
            "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}


def _read_temp(p: Path) -> float | None:
    try:
        v = int(p.read_text().strip()) / 1000
    except (OSError, ValueError):
        return None
    return v if 0 < v < 150 else None


def cpu_temp(root: Path | None = None) -> float | None:
    """Température la plus haute du CPU (°C), lue dans /sys. None si la machine ne la donne pas."""
    root = root or SYS
    best: float | None = None
    for hw in sorted((root / "hwmon").glob("hwmon*")):
        try:
            name = (hw / "name").read_text().strip()
        except OSError:
            continue
        if name not in CPU_SENSORS:
            continue
        for f in hw.glob("temp*_input"):
            t = _read_temp(f)
            if t is not None and (best is None or t > best):
                best = t
    if best is None:
        for z in sorted((root / "thermal").glob("thermal_zone*")):
            t = _read_temp(z / "temp")
            if t is not None and (best is None or t > best):
                best = t
    return round(best, 1) if best is not None else None


def memory(path: Path | None = None) -> dict | None:
    try:
        lines = (path or PROC_MEMINFO).read_text().splitlines()
    except OSError:
        return None
    kb = {}
    for ln in lines:
        k, _, v = ln.partition(":")
        try:
            kb[k] = int(v.split()[0])
        except (IndexError, ValueError):
            pass
    total, avail = kb.get("MemTotal"), kb.get("MemAvailable")
    if not total or avail is None:
        return None
    return {"pct": round(100 * (total - avail) / total, 1), "free_gb": round(avail / 1e6, 1),
            "total_gb": round(total / 1e6, 1)}


def load() -> dict:
    try:
        one = os.getloadavg()[0]
    except OSError:
        one = 0.0
    n = os.cpu_count() or 1
    return {"load": round(one, 2), "cores": n, "pct": round(100 * one / n)}


def ping(url: str, timeout: float = 5) -> bool:
    try:
        return httpx.get(url, timeout=timeout).is_success
    except Exception:
        return False


# ---------- Signes de vie et plantages ----------

def beat(role: str) -> None:
    (_dir() / f"{role}.beat").write_text(str(time.time()))


def beat_age(role: str) -> float | None:
    """Secondes depuis le dernier signe de vie du processus, None s'il n'en a jamais donné."""
    try:
        return max(0.0, time.time() - float((_dir() / f"{role}.beat").read_text()))
    except (OSError, ValueError):
        return None


def started(role: str) -> int:
    """À appeler au démarrage. Renvoie le nombre de plantages de la dernière heure (0 = arrêt normal)."""
    marker = _dir() / f"{role}.running"
    crashed = marker.exists()
    marker.write_text(str(time.time()))
    beat(role)
    if not crashed:
        return 0
    f = _dir() / f"{role}.crashes"
    try:
        crashes = [float(x) for x in f.read_text().split()]
    except (OSError, ValueError):
        crashes = []
    crashes = [t for t in crashes if t > time.time() - 7 * 86400] + [time.time()]
    f.write_text("\n".join(str(t) for t in crashes))
    return sum(t > time.time() - 3600 for t in crashes)


def stopped(role: str) -> None:
    """À appeler lors d'un arrêt normal (mise à jour, redémarrage demandé)."""
    (_dir() / f"{role}.running").unlink(missing_ok=True)


def crashes(role: str, hours: float = 24) -> int:
    try:
        return sum(float(x) > time.time() - hours * 3600 for x in (_dir() / f"{role}.crashes").read_text().split())
    except (OSError, ValueError):
        return 0


def crash_message(role: str, recent: int) -> str:
    what = {"worker": "Le worker (enregistrement, montage, publication)", "web": "L'interface web"}.get(role, role)
    msg = (f"⚠️ {what} s'est arrêté brutalement (plantage, manque de mémoire ou coupure de courant) "
           "et vient de redémarrer.")
    if recent >= 3:
        msg += f"\n🔁 {recent} plantages en 1 h : il redémarre en boucle, regarde les logs (docker compose logs --tail 100)."
    return msg


# ---------- Problèmes et alertes ----------

_fails: dict[str, int] = {}


def _down(name: str, failed: bool) -> bool:
    _fails[name] = _fails.get(name, 0) + 1 if failed else 0
    return _fails[name] >= 2


def worker_problems(threads=()) -> dict[str, str]:
    """Vérifications faites par le worker : machine, interface web, IA, tâches de fond."""
    cfg = get_settings()
    out: dict[str, str] = {}
    d = disk()
    if d["pct"] >= cfg.health_disk_pct or d["free_gb"] < cfg.health_disk_min_gb:
        out["disk"] = (f"💾 Disque presque plein : {d['pct']:.0f} % utilisé, {d['free_gb']} Go libres. "
                       "Les enregistrements vont échouer si ça continue (supprime de vieux clips ou réduis CLIP_KEEP_DAYS).")
    t = cpu_temp()
    if t is not None and t >= cfg.health_temp_max:
        out["temp"] = (f"🔥 CPU à {t:.0f} °C (seuil {cfg.health_temp_max:.0f} °C). "
                       "Vérifie que les aérations du laptop ne sont pas bouchées.")
    m = memory()
    if m and m["pct"] >= cfg.health_ram_pct:
        out["ram"] = (f"🧠 Mémoire presque pleine : {m['pct']:.0f} % utilisée, {m['free_gb']} Go libres. "
                      "Risque de plantage (trop de lives en même temps ?).")
    # deux vérifications ratées de suite avant d'alerter (un service qui redémarre après une mise à jour)
    if cfg.health_web_url and _down("web", not ping(cfg.health_web_url)):
        out["web"] = "🌐 L'interface web ne répond plus (conteneur web arrêté ou planté)."
    if cfg.ollama_url and _down("ollama", not ping(cfg.ollama_url.rstrip("/") + "/api/version")):
        out["ollama"] = "🤖 L'IA locale (Ollama) ne répond plus : les titres et le choix des moments sont moins bons."
    dead = [t.name for t in threads if not t.is_alive()]
    if dead:
        out["threads"] = "🧵 Une tâche de fond du worker s'est arrêtée : " + ", ".join(dead) + ". Redémarre le worker."
    return out


def web_problems() -> dict[str, str]:
    """Vérification faite par l'interface web : le worker donne-t-il signe de vie ?"""
    age = beat_age("worker")
    limit = get_settings().health_worker_timeout
    if age is not None and age > limit:
        return {"worker": f"🛑 Le worker ne donne plus signe de vie depuis {age / 60:.0f} min "
                          "(plus d'enregistrement ni de publication). Regarde : docker compose ps"}
    return {}


class Alerter:
    """Envoie chaque problème une fois, le rappelle de temps en temps, et prévient quand il est réglé."""

    def __init__(self, role: str):
        self.file = _dir() / f"alerts-{role}.json"

    def state(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def update(self, problems: dict[str, str], at: float | None = None) -> list[str]:
        """Renvoie les messages envoyés."""
        at = at or time.time()
        repeat = get_settings().health_repeat_hours * 3600
        state, sent = self.state(), []
        for key, msg in problems.items():
            last = state.get(key, {}).get("sent", 0)
            if not last or (repeat > 0 and at - last >= repeat):
                sent.append(("" if not last else "⏰ Toujours en cours : ") + msg)
                state[key] = {"since": state.get(key, {}).get("since", at), "sent": at, "msg": msg}
            else:
                state[key]["msg"] = msg
        for key in [k for k in state if k not in problems]:
            sent.append("✅ Réglé : " + state.pop(key)["msg"].split(" : ")[0].rstrip(". "))
        for msg in sent:
            notify.send(msg)
        try:
            self.file.write_text(json.dumps(state))
        except OSError as e:
            log.warning("État des alertes non enregistré : %s", e)
        return sent


def write_status(problems: dict[str, str], ollama_ok: bool | None) -> None:
    (_dir() / "status.json").write_text(json.dumps({"at": time.time(), "problems": problems, "ollama": ollama_ok}))


def snapshot() -> dict:
    """Tout ce qu'affiche le tableau de bord."""
    cfg = get_settings()
    try:
        last = json.loads((_dir() / "status.json").read_text())
    except (OSError, ValueError):
        last = {}
    problems = dict(last.get("problems") or {})
    problems.update(web_problems())
    age = beat_age("worker")
    return {"disk": disk(), "temp": cpu_temp(), "ram": memory(), "load": load(),
            "worker_age": age, "worker_ok": age is not None and age <= cfg.health_worker_timeout,
            "ollama": last.get("ollama"), "crashes": crashes("worker") + crashes("web"),
            "problems": list(problems.values()), "cfg": cfg}


def loop(role: str, stop, threads=()) -> None:
    """Boucle de surveillance (worker ou web). `stop` : threading.Event."""
    cfg = get_settings()
    alerter = Alerter(role)
    next_check = time.monotonic() + 60  # laisse le reste démarrer
    while not stop.is_set():
        try:
            beat(role)
            if cfg.health and time.monotonic() >= next_check:
                next_check = time.monotonic() + cfg.health_interval
                problems = worker_problems(threads) if role == "worker" else web_problems()
                if role == "worker":
                    write_status(problems, None if not cfg.ollama_url else "ollama" not in problems)
                alerter.update(problems)
        except Exception:
            log.exception("Surveillance du serveur")
        stop.wait(60)
