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
