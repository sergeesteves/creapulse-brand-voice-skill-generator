import os
import sys
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("CRAWL4AI_TOKEN", "test-token")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test-voice-skill.db")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MIN_URLS", "3")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_store():
    """Chaque test repart d'un store SQLite vide."""
    from app import store as store_mod
    path = "./test-voice-skill.db"
    if store_mod._store is not None:
        store_mod._store.close()
        store_mod._store = None
    for suffix in ("", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    yield
    if store_mod._store is not None:
        store_mod._store.close()
        store_mod._store = None
