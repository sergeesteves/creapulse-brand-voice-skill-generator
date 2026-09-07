import pytest
from fastapi.testclient import TestClient

from app import gating
from app.config import settings
from app.distill import Guide
from app.scraper import PageText, word_count
from app.store import get_store

PROSE = "\n\n".join([("Bon, pour faire simple, voici une phrase de prose qui compte des mots utiles pour la voix éditoriale. " * 5).strip()] * 8)
GUIDE_MD = "\n\n".join(f"## {s}\n- **trait** : « exemple »" for s in
                       ["Ton", "Style de phrase", "Vocabulaire", "Structure / format",
                        "Principes édito / positionnement"]) + "\n\n## Résumé en une phrase\nUne voix orale et directe."


@pytest.fixture
def client(monkeypatch):
    from app import main

    async def fake_fetch(urls, client=None):
        return [PageText(u, "Titre " + u[-1], PROSE, word_count(PROSE), True, "", "fr") for u in urls]

    async def fake_distill(pages, name, client=None):
        return Guide(GUIDE_MD, {}, "Une voix orale et directe.", "fake-model", {"total_tokens": 10})

    monkeypatch.setattr(main, "fetch_pages", fake_fetch)
    monkeypatch.setattr(main, "distill", fake_distill)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    with TestClient(main.app) as c:
        yield c


def test_store_counters_are_atomic_and_reversible():
    s = get_store()
    assert s.increment("global", "all") == 1
    assert s.increment("global", "all") == 2
    s.decrement("global", "all")
    assert s.get("global", "all") == 1
    s.upsert_lead("Jane@Example.com", consent=True)
    s.mark_verified("jane@example.com")
    lead = s.get_lead("jane@example.com")
    assert lead["verified_at"] and lead["consent_at"] and lead["generations"] == 0


def test_signed_tokens_roundtrip():
    assert gating.read_anon(gating.sign_anon(2)) == 2
    assert gating.read_anon("garbage") == 0
    assert gating.read_session(gating.sign_session("a@b.fr")) == "a@b.fr"
    assert gating.read_magic_token(gating.make_magic_token("a@b.fr")) == "a@b.fr"
    assert gating.read_magic_token("nope") is None


def test_anonymous_gets_one_generation_then_needs_email(client):
    r = client.post("/api/generate", json={"urls": ["https://example.org/article-1"]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["name"] == "Example" and data["slug"] == "example"
    assert data["prompt_block"].startswith("# Voix de marque — Example")
    assert data["skill_md"].startswith("---\nname: voix-example")
    assert data["download_available"] is False
    assert gating.ANON_COOKIE in r.cookies

    r2 = client.post("/api/generate", json={"urls": ["https://example.org/article-2"]})
    assert r2.status_code == 403 and r2.json()["code"] == "need_email"
    # rien n'a été consommé pour la 2e tentative
    assert get_store().get("global", "all") == 1


def test_ip_cap_blocks_cookie_clearing(client):
    client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    client.cookies.clear()
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 200  # 2e génération par IP (cap = 2)
    client.cookies.clear()
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 403 and r.json()["code"] == "need_email"


def test_magic_link_flow_unlocks_verbatim_and_download(client):
    r = client.post("/api/magic-link", json={"email": "jane@example.com", "consent": True})
    assert r.status_code == 200, r.text
    link = r.json()["debug_link"]
    r = client.get(link.replace(settings.public_base_url, ""), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?connected=1"
    assert gating.SESSION_COOKIE in r.cookies

    r = client.post("/api/generate", json={"urls": ["https://example.org/a", "https://example.org/b"], "verbatim": True})
    data = r.json()
    assert r.status_code == 200 and data["connected"] and data["download_available"]
    assert data["verbatim"]["applied"] and "## Extraits de style (verbatim)" in data["skill_md"]

    r = client.post("/api/download/skill", json={"skill_md": data["skill_md"], "slug": data["slug"]})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert get_store().get_lead("jane@example.com")["generations"] == 1


def test_magic_link_requires_consent_and_valid_email(client):
    assert client.post("/api/magic-link", json={"email": "jane@example.com", "consent": False}).status_code == 400
    assert client.post("/api/magic-link", json={"email": "not-an-email", "consent": True}).status_code == 400


def test_global_cap(client, monkeypatch):
    monkeypatch.setattr(settings, "global_daily_cap", 1)
    client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    client.cookies.clear()
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 429 and r.json()["code"] == "global_cap"


def test_input_error_releases_slot(client, monkeypatch):
    from app import main

    async def short_fetch(urls, client=None):
        return [PageText(u, "", "", 12, False, "texte trop court (12 mots)") for u in urls]

    monkeypatch.setattr(main, "fetch_pages", short_fetch)
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 422 and "trop court" in r.json()["message"]
    assert get_store().get("global", "all") == 0


def test_health_and_index(client):
    assert client.get("/health").json()["status"] == "ok"
    html = client.get("/").text
    assert "Générateur de skill de voix de marque IA" in html
