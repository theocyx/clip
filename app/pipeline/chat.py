"""Chat Twitch enregistré pendant les lives : les rafales de messages (« MDR », « KEKW », « !!! »)
signalent les moments forts mieux que le son seul.

Connexion anonyme en lecture (pas de compte nécessaire). Chaque message est écrit dans
`chat.tsv` (horodatage Unix, texte) ; `rec_starts.txt` garde l'heure de début de chaque enregistrement."""
from __future__ import annotations

import logging
import random
import re
import socket
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)
HOST, PORT = "irc.chat.twitch.tv", 6667
# Réactions fortes du chat (emotes, rires, surprise)
HYPE = re.compile(
    r"\b(kekw|lul|lulw|omegalul|pog|pogu|poggers|pogchamp|monkas|sadge|ez|clip|mdr+|ptdr+|xd+|lol+|wtf|"
    r"hahah*|ahah+|jpp|dingue|quoi+|omg|gg|w+)\b|[!?]{2,}|😂|🤣|💀|🔥|😭", re.I)
_log_lock = threading.Lock()
_loggers: dict[int, "ChatLogger"] = {}


def twitch_channel(url: str) -> str:
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url or "")
    return m.group(1).lower() if m and m.group(1).lower() != "videos" else ""


class ChatLogger(threading.Thread):
    def __init__(self, channel: str, out: Path):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel, self.out = channel, out
        self.halt = threading.Event()

    def run(self) -> None:
        wait = 5
        while not self.halt.is_set():
            try:
                self._session()
                wait = 5
            except Exception as e:
                log.debug("Chat %s : %s", self.channel, e)
            self.halt.wait(wait)
            wait = min(120, wait * 2)

    def _session(self) -> None:
        with socket.create_connection((HOST, PORT), timeout=30) as sock, open(self.out, "a", encoding="utf-8") as f:
            sock.settimeout(30)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            buf = b""
            while not self.halt.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    sock.sendall(b"PING :tmi.twitch.tv\r\n")
                    continue
                if not data:
                    return
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                    elif " PRIVMSG #" in line:
                        text = line.split(" :", 1)[1] if " :" in line else ""
                        f.write(f"{time.time():.2f}\t{text.replace(chr(9), ' ')}\n")
                f.flush()


def start(video_id: int, url: str, work: Path) -> None:
    """Commence à enregistrer le chat d'un live Twitch (rien pour YouTube)."""
    channel = twitch_channel(url)
    if not channel:
        return
    stop(video_id)
    work.mkdir(parents=True, exist_ok=True)
    with open(work / "rec_starts.txt", "a") as f:
        f.write(f"{time.time():.2f}\n")
    t = ChatLogger(channel, work / "chat.tsv")
    with _log_lock:
        _loggers[video_id] = t
    t.start()


def stop(video_id: int) -> None:
    with _log_lock:
        t = _loggers.pop(video_id, None)
    if t:
        t.halt.set()


def activity(work: Path, n: int, delay: float = 6.0) -> np.ndarray | None:
    """Activité du chat par seconde de la vidéo (messages + 2 × réactions fortes), décalée de `delay` s
    (les spectateurs réagissent après avoir vu la scène). None si pas de chat exploitable
    (pas de chat, ou live recollé après un redémarrage : l'heure de chaque morceau est inconnue)."""
    chat, starts = work / "chat.tsv", work / "rec_starts.txt"
    if n <= 0 or not chat.exists() or not starts.exists():
        return None
    marks = [ln for ln in starts.read_text().split() if ln.strip()]
    if len(marks) != 1:
        return None
    t0 = float(marks[0])
    out = np.zeros(n, dtype=np.float32)
    count = 0
    for line in chat.read_text(encoding="utf-8", errors="replace").splitlines():
        ts, _, text = line.partition("\t")
        try:
            i = int(float(ts) - t0 - delay)
        except ValueError:
            continue
        if 0 <= i < n:
            out[i] += 1 + 2 * len(HYPE.findall(text)[:3])
            count += 1
    return out if count >= 20 else None
