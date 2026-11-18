import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("OLLAMA_URL", "")
os.environ.setdefault("GTA6_MODE", "off")  # sinon le mode s'active selon la vraie date et fausse les tests

import pytest


@pytest.fixture(autouse=True)
def _gta6_override_reset():
    """Le choix du mode GTA 6 fait par un test (fichier data/gta6_mode) ne doit pas déborder sur les suivants."""
    yield
    from app.config import get_settings
    (get_settings().data_dir / "gta6_mode").unlink(missing_ok=True)
