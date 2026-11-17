import json

import numpy as np

from app import db
from app.models import Clip, ClipInfo, Post, Video
from app.pipeline import highlights, llm, sponsor
from app.config import get_settings

from .test_pipeline import make_segments


def test_detects_french_sponsor_phrases():
    for t in ["Cette vidéo est sponsorisée par NordVPN", "avec mon code PIERRE vous avez -20 % sur",
              "merci à Raid Shadow Legends pour avoir sponsorisé", "le lien est en description",
              "un partenariat avec la marque", "Code promo dans la bio"]:
        assert sponsor.is_sponsor_text(t), t
    for t in ["oh non il m'a tué", "c'est incroyable regarde ça", "on part sur le braquage de la banque"]:
        assert not sponsor.is_sponsor_text(t), t


def test_transcript_ranges_merge():
    segs = make_segments(600)
    segs[25]["text"] = "Cette vidéo est sponsorisée par Machin"
    segs[30]["text"] = "avec mon code PROMO"
    r = sponsor.from_transcript(segs, before=10, after=30)
    assert len(r) == 1
    a, b = r[0]
    assert a <= segs[25]["start"] - 9 and b >= segs[30]["end"] + 29


def test_find_moments_avoids_excluded():
    total = 600
    loud = np.full(total, -30.0, dtype=np.float32)
    loud[400:420] = -8.0
    loud[100:110] = -15.0
    segs = make_segments(total)
    m = highlights.find_moments(loud, segs, count=1, target=60, min_len=35, max_len=180, exclude=[(370, 470)])
    assert m and all(x.end <= 372 or x.start >= 468 for x in m)


def test_llm_pub_flag_zeroes_score():
    assert llm.parse('{"pub": true, "score": 8, "title": "t", "hook": "h"}')[0] == 0
    assert llm.parse('{"pub": false, "score": 8, "title": "t", "hook": "h"}')[0] == 8


def test_sponsorblock(monkeypatch, tmp_path):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"segment": [10.0, 70.5], "category": "sponsor", "actionType": "skip"}]
    calls = []
    monkeypatch.setattr(sponsor.httpx, "get", lambda *a, **k: calls.append(k) or R())
    cache = tmp_path / "sb.json"
    assert sponsor.from_sponsorblock("youtube:abc", cache) == [(10.0, 70.5)]
    assert sponsor.from_sponsorblock("youtube:abc", cache) == [(10.0, 70.5)]
    assert len(calls) == 1
    assert sponsor.from_sponsorblock("twitch:123") == []


def test_purge_queue_cancels_pending(monkeypatch):
    monkeypatch.setattr(sponsor, "from_sponsorblock", lambda *a, **k: [])
    with db.session() as s:
        v = Video(key="twitch:sponsor-test", url="u", title="live")
        s.add(v)
        s.commit()
        vid = v.id
    d = get_settings().videos_dir / str(vid)
    d.mkdir(parents=True, exist_ok=True)
    segs = make_segments(600)
    segs[50]["text"] = "Merci à Machin qui sponsorise cette vidéo, code promo dans la description"
    (d / "transcript.json").write_text(json.dumps(segs))
    with db.session() as s:
        bad = Clip(video_id=vid, group="gbad", start=190, end=250, path="/nope.mp4")
        good = Clip(video_id=vid, group="ggood", start=450, end=510, path="/nope.mp4")
        s.add(bad), s.add(good)
        s.commit()
        for c in (bad, good):
            s.add(ClipInfo(clip_id=c.id, data=json.dumps({"text": "bla"})))
            s.add(Post(clip_id=c.id, platform="youtube"))
        s.commit()
        bad_id, good_id = bad.id, good.id
    assert sponsor.purge_queue(ocr=False) == 1
    with db.session() as s:
        from sqlmodel import select
        st = {p.clip_id: p.status for p in s.exec(select(Post)).all()}
    assert st[bad_id] == "cancelled" and st[good_id] == "pending"
    assert sponsor.purge_queue(ocr=False) == 0
