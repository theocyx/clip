from types import SimpleNamespace

from app.pipeline import render


def test_gpu_failure_falls_back_to_cpu(monkeypatch, tmp_path):
    monkeypatch.setattr(render.get_settings(), "video_codec", "h264_vaapi")
    monkeypatch.setattr(render, "_gpu_broken", False)
    codecs = []

    def fake_run(cmd, **kw):
        codec = cmd[cmd.index("-c:v") + 1]
        codecs.append(codec)
        return SimpleNamespace(returncode=1 if codec.endswith("_vaapi") else 0, stderr="No VA display found")

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    render.render(tmp_path / "s.mp4", 0, 10, "x", tmp_path / "o" / "c1.mp4")
    render.render(tmp_path / "s.mp4", 0, 10, "x", tmp_path / "o" / "c2.mp4")
    # 1er clip : essai GPU puis CPU ; 2e clip : CPU directement
    assert codecs == ["h264_vaapi", "libx264", "libx264"]


def test_auto_codec_uses_gpu_only_if_available(monkeypatch):
    s = render.get_settings()
    monkeypatch.setattr(s, "video_codec", "auto")
    monkeypatch.setattr(render.os, "access", lambda p, m: True)
    assert render.resolve_codec() == "h264_vaapi"
    monkeypatch.setattr(render.os, "access", lambda p, m: False)
    assert render.resolve_codec() == "libx264"
    assert render.resolve_codec("libx265") == "libx265"


def test_two_pass_audio_filter():
    assert render.audio_filter(None) == render.AUDIO
    m = {"input_i": "-23.8", "input_tp": "-20", "input_lra": "1.7", "input_thresh": "-35.7", "target_offset": "0.1"}
    f = render.audio_filter(m)
    assert "measured_I=-23.8" in f and "linear=true" in f
    cmd = render.build_cmd(render.Path("s.mp4"), 0, 10, "a.ass", render.Path("o.mp4"), "libx264", loud=m)
    af = cmd[cmd.index("-af") + 1]
    assert af.endswith(f) and af.startswith(render.SYNC)  # son calé sur l'image, puis normalisé


def test_measure_loudness_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("ffmpeg absent")
    monkeypatch.setattr(render.subprocess, "run", boom)
    assert render.measure_loudness(render.Path("s.mp4"), [(0, 5)]) is None


def test_upload_quality_settings(monkeypatch):
    """Fichier envoyé : son 48 kHz 192k, couleurs BT.709 déclarées, GPU moins compressé, agrandissements nets."""
    from dataclasses import dataclass
    from pathlib import Path

    from app.config import get_settings
    from app.pipeline import render

    @dataclass
    class Box:
        x: float
        y: float
        w: float
        h: float

    cfg = get_settings()
    monkeypatch.setattr(cfg, "crf", 21)
    monkeypatch.setattr(cfg, "sharpen", 0.5)
    gpu = render.build_cmd(Path("in.mp4"), 0, 30, "s.ass", Path("out.mp4"), codec="h264_vaapi",
                           cam=Box(0.75, 0.05, 0.22, 0.25))
    j = " ".join(gpu)
    assert "-qp 20" in j and "-ar 48000" in j and "-b:a 192k" in j and "-colorspace bt709" in j
    assert "flags=lanczos" in j and "unsharp=5:5:0.50" in j and "unsharp=5:5:0.30" in j
    cpu = " ".join(render.build_cmd(Path("in.mp4"), 0, 30, "s.ass", Path("out.mp4"), codec="libx264"))
    assert "-crf 21" in cpu and "-color_primaries bt709" in cpu and "unsharp" not in cpu  # vidéo réduite : pas d'accentuation
    monkeypatch.setattr(cfg, "sharpen", 0)
    assert render.sharpen() == ""


def test_gaps_in_live_recording_keep_sound_in_sync(tmp_path):
    """Enregistrement de live avec un trou de 3 s (morceaux perdus) : la transcription et le rendu doivent
    utiliser la même ligne de temps, sinon sous-titres et coupures des blancs tombent à côté (son haché)."""
    import subprocess
    import wave

    import numpy as np

    from app.pipeline import media

    def seg(color, beep_at, offset, out):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=30:d=10",
                        "-f", "lavfi", "-i", f"sine=f=1000:d=10,volume='if(between(t,{beep_at},{beep_at}+0.5),1,0)'"
                        ":eval=frame", "-c:v", "libx264", "-g", "30", "-c:a", "aac", "-f", "mpegts",
                        "-output_ts_offset", str(offset), str(out)], check=True)

    seg("red", 8, 0, tmp_path / "a.ts")
    seg("blue", 5, 13, tmp_path / "b.ts")  # reprend à 13 s : 3 s perdues
    live = tmp_path / "live.ts"
    live.write_bytes((tmp_path / "a.ts").read_bytes() + (tmp_path / "b.ts").read_bytes())

    def beeps(path):
        wav = tmp_path / "x.wav"
        media.extract_audio(path, wav)
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
        env = np.array([np.abs(x[i:i + sr // 10]).mean() for i in range(0, len(x), sr // 10)])
        on = set(np.where(env > 800)[0].tolist())
        return [i / 10 for i in sorted(on) if i - 1 not in on]  # débuts des bips (en s)

    assert beeps(live) == [8.0, 18.0]  # 2e bip à 13 + 5 s, comme dans la vidéo (et pas à 15 s)
    # clip sans les blancs autour des deux bips : chaque bip doit tomber dans son morceau
    ass = ("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
           "Style: Sub,Arial,74\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
           "Effect, Text\n")
    out = render.render(live, 7, 19, ass, tmp_path / "clip.mp4", cuts=[(7, 9), (17, 19)])
    assert [round(abs(x - y), 2) <= 0.1 for x, y in zip(beeps(out), [1.0, 3.0])] == [True, True]  # pas de 0,1 s


def test_live_cuts_are_frame_exact_and_in_sync(tmp_path):
    """Live en MPEG-TS (sans index, image clé toutes les 2 s) : un « -ss » direct tombait après l'image clé,
    d'où image figée au début du clip, vidéo qui débordait après chaque morceau et son décalé / troué après
    chaque coupure de blanc. Chaque flash blanc doit tomber en même temps que son bip, au bon endroit, et la
    vidéo doit durer exactement la somme des morceaux."""
    import subprocess

    import numpy as np

    src = tmp_path / "live.ts"
    flashes = "+".join(f"between(t,{k},{k}+0.3)" for k in (4.5, 8.5, 13.5))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=black:s=160x120:r=30:d=16,drawbox=c=white:t=fill:enable='{flashes}'",
                    "-f", "lavfi", "-i", f"sine=f=1000:d=16:sample_rate=48000,volume='if({flashes},1,0)':eval=frame",
                    "-c:v", "libx264", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-c:a", "aac",
                    "-f", "mpegts", "-output_ts_offset", "1000", str(src)], check=True)
    ass = ("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
           "Style: Sub,Arial,74\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
           "Effect, Text\n")
    cuts = [(3.3, 5.3), (7.7, 9.9), (12.6, 14.4)]  # débuts loin des images clés (0, 2, 4… s)
    out = render.render(src, 3.3, 14.4, ass, tmp_path / "clip.mp4", cuts=cuts)
    x = np.abs(np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "f32le", "-ac", "1",
                                             "-ar", "8000", "-"], capture_output=True).stdout, np.float32))
    env = np.array([x[i:i + 40].max() for i in range(0, len(x), 40)])  # pas de 5 ms
    sound = [i * 0.005 for i in range(4, len(env)) if env[i] > 0.2 and env[i - 4:i].max() <= 0.2]
    y = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", "scale=16:16", "-f", "rawvideo",
                                      "-pix_fmt", "gray", "-"], capture_output=True).stdout, np.uint8)
    y = y.reshape(-1, 256).mean(axis=1)
    image = [i / 30 for i in range(1, len(y)) if y[i] > 100 and y[i - 1] <= 100]
    expected = [1.2, 2.0 + 0.8, 2.0 + 2.2 + 0.9]  # 4,5 s, 8,5 s et 13,5 s dans le clip monté
    assert len(sound) == 3 and len(image) == 3
    assert all(abs(a - e) <= 0.06 for a, e in zip(image, expected)), image
    assert all(abs(a - v) <= 0.05 for a, v in zip(sound, image)), (sound, image)  # son calé sur l'image
    assert abs(len(y) / 30 - sum(b - a for a, b in cuts)) <= 0.05  # pas de débordement de la vidéo
