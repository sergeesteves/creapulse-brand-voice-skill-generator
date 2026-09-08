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
    monkeypatch.setattr(settings, "min_urls", 1)  # ces tests portent sur le gating, pas sur le nombre d'URLs
    with TestClient(main.app) as c:
        yield c


def test_api_enforces_min_urls(client, monkeypatch):
    monkeypatch.setattr(settings, "min_urls", 3)
    r = client.post("/api/generate", json={"urls": ["https://example.org/a", "https://example.org/b"]})
    assert r.status_code == 400 and "au moins 3" in r.json()["message"]
    assert get_store().get("global", "all") == 0
    html = client.get("/").text
    assert html.count('type="url" name="url"') == 3 and "3 à 5 URLs" in html


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


def test_direct_mode_without_smtp_unlocks_immediately(client):
    assert settings.mail_mode == "direct"
    r = client.post("/api/magic-link", json={"email": "jane@example.com", "consent": True})
    assert r.status_code == 200 and r.json()["connected"] is True
    assert gating.SESSION_COOKIE in r.cookies
    lead = get_store().get_lead("jane@example.com")
    assert lead["consent_at"] and lead["verified_at"] is None  # capturé, pas vérifié
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 200 and r.json()["download_available"]


def test_magic_link_flow_unlocks_download(client, monkeypatch):
    from app import main
    sent = {}
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(main, "send_magic_link", lambda to, link: sent.update(to=to, link=link))
    r = client.post("/api/magic-link", json={"email": "jane@example.com", "consent": True})
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is False and sent["to"] == "jane@example.com"
    assert gating.SESSION_COOKIE not in r.cookies
    r = client.get(sent["link"].replace(settings.public_base_url, ""), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?connected=1"
    assert gating.SESSION_COOKIE in r.cookies
    assert get_store().get_lead("jane@example.com")["verified_at"]

    r = client.post("/api/generate", json={"urls": ["https://example.org/a", "https://example.org/b"]})
    data = r.json()
    assert r.status_code == 200 and data["connected"] and data["download_available"]
    assert "verbatim" not in data["skill_md"].lower().split("## ton")[0]  # plus d'option extraits

    r = client.post("/api/download/skill", json={"skill_md": data["skill_md"], "slug": data["slug"]})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    r = client.post("/api/download/skill", json={"skill_md": data["skill_md"], "slug": data["slug"], "format": "zip"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert names == ["voix-example/SKILL.md"]
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
    assert 'href="/static/style.css?v=' in html and 'const ROOT = "";' in html


def test_embed_mode_strips_chrome_and_reports_height(client):
    html = client.get("/?embed=1").text
    assert '<body class="embed">' in html and "const EMBED = true;" in html and "vsg-height" in html
    assert '<body class="">' in client.get("/").text


def test_root_path_prefixes_links_and_redirects(client, monkeypatch):
    monkeypatch.setattr(settings, "root_path", "/outils/voix-de-marque")
    html = client.get("/").text
    assert 'href="/outils/voix-de-marque/static/style.css?v=' in html
    assert 'const ROOT = "/outils/voix-de-marque";' in html
    r = client.get("/auth/verify?token=bad", follow_redirects=False)
    assert r.headers["location"] == "/outils/voix-de-marque/?auth=invalid"
    r = client.get("/static/style.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    assert client.get("/static/..%2Fmain.py").status_code == 404
    assert client.get("/static/nope.css").status_code == 404
