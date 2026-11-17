import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import db
from app.models import Clip, Post, Video
from app.pipeline import process, sources, transcribe
from app.web.main import app, auth, video_steps


def test_download_progress_parsing():
    t = sources.DownloadProgress()
    tag = sources.PROGRESS_TAG
    assert t.feed("[download] Destination: x") is None
    assert t.feed(f"{tag} 137 50 100 NA") == 45
    assert t.feed(f"{tag} 137 100 100 NA") == 90
    assert t.feed(f"{tag} 140 NA NA NA") is None
    assert t.feed(f"{tag} 140 5 NA 10") == 95
    assert t.feed(f"{tag} 140 10 10 NA") == 99  # 100 % seulement après la fusion


def test_download_reports_progress(tmp_path, monkeypatch):
    script = (f"import pathlib;print('{sources.PROGRESS_TAG} 1 5 10 NA', flush=True);"
              f"pathlib.Path(r'{tmp_path}/source.mp4').write_bytes(b'x')")
    monkeypatch.setattr(sources, "download_cmd", lambda url, out, live: [sys.executable, "-c", script])
    seen = []
    sources.download("u", tmp_path, on_progress=seen.append)
    assert seen[-1] == 100


def test_transcribe_progress(monkeypatch, tmp_path):
    segs = [SimpleNamespace(start=i * 60, end=i * 60 + 60, text="a", words=[]) for i in range(3)]
    monkeypatch.setattr(transcribe, "_get_model", lambda: SimpleNamespace(transcribe=lambda *a, **k: (iter(segs), None)))
    seen = []
    from tests.test_transcribe import make_wav
    transcribe.transcribe(make_wav(tmp_path / "a.wav", 1), tmp_path / "t.json", 600, seen.append)
    assert seen == [10, 20, 30, 100]


def test_progress_saved_and_reset():
    with db.session() as s:
        v = Video(key="prog:1", url="u", status="transcribing")
        s.add(v)
        s.commit()
    p = process.Progress(v.id)
    p(42.26)
    p(50)  # trop tôt : ignoré
    with db.session() as s:
        assert s.get(Video, v.id).progress == 42.3
    process.set_status(v.id, "analyzing")
    with db.session() as s:
        assert s.get(Video, v.id).progress == 0


def test_video_steps():
    st = video_steps(Video(key="k", url="u", status="analyzing", progress=40))
    assert [x["state"] for x in st] == ["done", "done", "run", "todo", "todo"]
    assert st[2]["pct"] == 40
    assert video_steps(Video(key="k", url="u", status="recording"))[0]["state"] == "live"
    done = video_steps(Video(key="k", url="u", status="done"), (1, 4))
    assert done[4]["pct"] == 25 and done[4]["note"] == "1/4"
    assert video_steps(Video(key="k", url="u", status="failed")) is None


def test_progress_endpoint():
    with db.session() as s:
        v = Video(key="prog:2", url="u", status="done")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, group="prog-test", start=0, end=1)
        s.add(c)
        s.commit()
        s.add(Post(clip_id=c.id, platform="youtube", status="published"))
        s.add(Post(clip_id=c.id, platform="tiktok", status="failed"))
        s.commit()
    app.dependency_overrides[auth] = lambda: None
    try:
        client = TestClient(app)
        r = client.get(f"/videos/progress?ids={v.id},abc")
        page = client.get("/videos")
    finally:
        app.dependency_overrides.pop(auth, None)
    d = r.json()[str(v.id)]
    assert d["status"] == "done" and "1/2" in d["html"] and "Publication" in d["html"]
    assert page.status_code == 200 and f'data-progress="{v.id}"' in page.text
