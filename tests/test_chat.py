import socket
import threading
import time

import numpy as np

from app.pipeline import chat, highlights


def test_twitch_channel():
    assert chat.twitch_channel("https://www.twitch.tv/Squeezie") == "squeezie"
    assert chat.twitch_channel("https://www.twitch.tv/videos/123") == ""
    assert chat.twitch_channel("https://www.youtube.com/watch?v=x") == ""


def test_activity_maps_messages_to_video_seconds(tmp_path):
    t0 = 1_000_000.0
    (tmp_path / "rec_starts.txt").write_text(f"{t0}\n")
    lines = [f"{t0 + 10 + i * 0.1}\tsalut" for i in range(10)]
    lines += [f"{t0 + 56 + i * 0.05}\tKEKW MDRRR" for i in range(20)]
    (tmp_path / "chat.tsv").write_text("\n".join(lines) + "\n")
    act = chat.activity(tmp_path, 100, delay=6)
    assert act is not None
    assert act[4] == 10  # 10 messages simples à 10 s, décalés de 6 s
    assert act[50] == 20 * 5  # réactions fortes comptées en plus
    # live recollé après un redémarrage : heure des morceaux inconnue, chat ignoré
    (tmp_path / "rec_starts.txt").write_text(f"{t0}\n{t0 + 500}\n")
    assert chat.activity(tmp_path, 100) is None


def test_chat_burst_steers_moment_choice():
    n = 900
    loud = np.full(n, -30.0, dtype=np.float32)
    segs = [{"start": float(i), "end": float(i + 4), "text": "bon alors on continue.",
             "words": [{"start": float(i), "end": float(i + 0.5), "word": "bon"}]} for i in range(0, n - 4, 5)]
    talk = np.ones(n, dtype=np.float32)
    talk[600:620] = 40
    ms = highlights.find_moments(loud, segs, count=1, target=40, min_len=30, max_len=60, chat=talk)
    assert ms and ms[0].start <= 610 <= ms[0].end
    assert "chat" in ms[0].features


def test_logger_reads_irc(tmp_path, monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    monkeypatch.setattr(chat, "HOST", "127.0.0.1")
    monkeypatch.setattr(chat, "PORT", srv.getsockname()[1])

    def serve():
        conn, _ = srv.accept()
        conn.recv(1024)
        conn.sendall(b"PING :tmi.twitch.tv\r\n:a!a@a.tmi.twitch.tv PRIVMSG #x :KEKW quoi\r\n")
        time.sleep(0.5)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    chat.start(1, "https://www.twitch.tv/x", tmp_path)
    for _ in range(50):
        if (tmp_path / "chat.tsv").exists() and "KEKW" in (tmp_path / "chat.tsv").read_text():
            break
        time.sleep(0.1)
    chat.stop(1)
    assert "KEKW quoi" in (tmp_path / "chat.tsv").read_text()
    assert len((tmp_path / "rec_starts.txt").read_text().split()) == 1
