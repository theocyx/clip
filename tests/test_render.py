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
    assert f in cmd


def test_measure_loudness_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("ffmpeg absent")
    monkeypatch.setattr(render.subprocess, "run", boom)
    assert render.measure_loudness(render.Path("s.mp4"), [(0, 5)]) is None
