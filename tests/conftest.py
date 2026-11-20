import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OLLAMA_URL", "")
os.environ.setdefault("GTA6_MODE", "off")
# garde-fous anti-rafale coupés pour les anciens tests (testés à part dans test_quality.py)
for k, v in {"POST_GAP_FLOOR": "0", "DAILY_CAP": "0", "QUIET_HOURS": "", "SPREAD_POSTS": "false",
             "TOPIC_ONLY": "false", "PUBLISH_MIN_LLM": "0", "QUEUE_MAX_HOURS": "0",
             "PUBLISH_TOP_PCT": "0", "POLISH": "false", "NEWS_MAP": "false", "BURST_MINUTES": "0", "CLIP_MIN_LLM": "0"}.items():
    os.environ.setdefault(k, v)  # sinon le mode s'active selon la vraie date et fausse les tests

import pytest


@pytest.fixture(autouse=True)
def _gta6_override_reset():
    """Le choix du mode GTA 6 fait par un test (fichier data/gta6_mode) ne doit pas déborder sur les suivants."""
    yield
    from app.config import get_settings
    (get_settings().data_dir / "gta6_mode").unlink(missing_ok=True)
