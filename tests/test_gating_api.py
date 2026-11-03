import pytest
from fastapi.testclient import TestClient

from app import gating
from app.config import settings
from app.distill import Guide
from app.scraper import PageText, word_count
from app.store import get_store
from conftest import login

PROSE = "\n\n".join([("Bon, pour faire simple, voici une phrase de prose qui compte des mots utiles pour la voix éditoriale. " * 5).strip()] * 8)
GUIDE_MD = "\n\n".join(f"## {s}\n- **trait** : « exemple »" for s in
                       ["Ton", "Style de phrase", "Vocabulaire", "Structure / format",
                        "Principes édito / positionnement"]) + "\n\n## Résumé en une phrase\nUne voix orale et directe."


@pytest.fixture
def client(monkeypatch):
    from app import main

    async def fake_fetch(urls, client=None):
        return [PageText(u, "Titre " + u[-1], PROSE, word_count(PROSE), True, "", "fr") for u in urls]

    async def fake_distill(pages, name, client=None, meter=None):
        return Guide(GUIDE_MD, {}, "Une voix orale et directe.", "fake-model", {"total_tokens": 10})

    monkeypatch.setattr(main, "fetch_pages", fake_fetch)
    monkeypatch.setattr(main, "distill", fake_distill)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    monkeypatch.setattr(settings, "min_urls", 1)  # ces tests portent sur le gating, pas sur le nombre d'URLs
    with TestClient(main.app) as c:
        yield c


def test_api_enforces_min_urls(client, monkeypatch):
    monkeypatch.setattr(settings, "min_urls", 3)
    login(client)
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


def test_member_session_roundtrip():
    sess = gating.read_session(gating.sign_session("A@b.fr", 42, (1, 3), "Jane"))
    assert sess.email == "a@b.fr" and sess.user_id == 42 and sess.levels == (1, 3)
    assert gating.read_session("garbage") is None
    # une session sans identifiant de membre ne vaut rien : le porte-monnaie en a besoin
    forged = gating._serializer("member").dumps({"e": "a@b.fr", "l": []})
    assert gating.read_session(forged) is None


def test_without_an_account_nothing_runs(client, fake_wallet):
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 403 and r.json()["code"] == "need_login"
    assert client.get("/api/credits").status_code == 403
    assert client.post("/api/download/skill", json={"skill_md": "---\nname: voix-x", "slug": "x"}).status_code == 403
    html = client.get("/").text
    assert "Réservé aux membres" in html and 'id="form-card" hidden' in html
    assert fake_wallet.calls == [] and get_store().get("global", "all") == 0
    # plus de capture d'email ni de génération anonyme
    assert client.post("/api/magic-link", json={"email": "x@y.fr", "consent": True}).status_code in (404, 405)
    assert "email-form" not in html and "génération gratuite" not in html


def test_a_member_generation_debits_then_reports_its_cost(client, fake_wallet):
    login(client, user_id=7)
    r = client.post("/api/generate", json={"urls": ["https://example.org/article-1"]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["name"] == "Example" and data["download_available"] is True
    assert data["prompt_block"].startswith("# Voix de marque — Example")
    assert data["balance"] == 40
    [(_, debit)] = [c for c in fake_wallet.calls if c[0] == "debit"]
    assert debit["user_id"] == 7 and debit["tier"] == "generation"
    assert debit["usage_ref"].endswith(":1") and len(debit["usage_ref"]) <= 64
    # le distill factice n'appelle pas le modèle : pas de coût à déclarer
    assert "cost" not in fake_wallet.routes() and "refund" not in fake_wallet.routes()

    r = client.post("/api/download/skill", json={"skill_md": data["skill_md"], "slug": data["slug"], "format": "zip"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"


def test_two_generations_use_two_usage_refs(client, fake_wallet):
    login(client)
    client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    refs = [b["usage_ref"] for route, b in fake_wallet.calls if route == "debit"]
    assert len(refs) == 2 and refs[0] != refs[1] and fake_wallet.balance == 30


def test_not_enough_credits_shows_the_recharge_link(client, fake_wallet):
    fake_wallet.balance = 5
    login(client)
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 402
    body = r.json()
    assert body["code"] == "need_credits" and body["recharge_url"].startswith("https://staging.creapulse.fr/")
    assert "10 crédits" in body["message"] and "5" in body["message"]
    assert get_store().get("global", "all") == 0  # le plafond du jour n'est pas consommé


def test_credits_route_gives_balance_and_price(client, fake_wallet):
    login(client)
    data = client.get("/api/credits").json()
    assert data == {"ok": True, "balance": 50, "cost": 10, "can_start": True,
                    "recharge_url": "https://staging.creapulse.fr/credits/", "closed_reason": None, "message": None}


def test_a_refused_welcome_says_so_instead_of_a_bare_zero(client, fake_wallet, monkeypatch):
    """Adresse jetable ou plafond mensuel des bienvenues : solde à 0 et `welcome_closed`."""
    from app.wallet import WalletError
    fake_wallet.balance = 0
    real = fake_wallet.call

    async def with_reason(c, route, body):
        out = await real(c, route, body)
        return {**out, "closed_reason": "welcome_closed"} if route == "balance" else out

    monkeypatch.setattr("app.wallet.call", with_reason)
    login(client)
    data = client.get("/api/credits").json()
    assert data["can_start"] is False and "bienvenue" in data["message"]

    fake_wallet.fail("debit", WalletError(402, "credits_insufficient",
                                          {"balance": 0, "required": 10, "closed_reason": "welcome_closed",
                                           "recharge_url": "https://staging.creapulse.fr/credits/"}))
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 402 and "bienvenue" in r.json()["message"] and r.json()["recharge_url"]


def test_rejected_pages_are_not_debited(client, monkeypatch, fake_wallet):
    from app import main

    async def short_fetch(urls, client=None):
        return [PageText(u, "", "", 12, False, "texte trop court (12 mots)") for u in urls]

    monkeypatch.setattr(main, "fetch_pages", short_fetch)
    login(client)
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 422 and "trop court" in r.json()["message"]
    assert get_store().get("global", "all") == 0 and fake_wallet.calls == []


def test_account_cap(client, monkeypatch):
    monkeypatch.setattr(settings, "account_daily_cap", 1)
    login(client)
    assert client.post("/api/generate", json={"urls": ["https://example.org/a"]}).status_code == 200
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 429 and r.json()["code"] == "account_cap"


def test_global_cap(client, monkeypatch):
    monkeypatch.setattr(settings, "global_daily_cap", 1)
    login(client, "a@b.fr")
    client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    login(client, "c@d.fr", user_id=8)
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 429 and r.json()["code"] == "global_cap"


def test_health_and_index(client):
    assert client.get("/health").json()["status"] == "ok"
    html = client.get("/").text
    assert "Générateur de skill de voix de marque IA" in html
    assert 'href="/static/style.css?v=' in html and 'const ROOT = "";' in html


def test_embed_mode_strips_chrome_and_reports_height(client):
    html = client.get("/?embed=1").text
    assert '<body class="embed">' in html and "const EMBED = true;" in html and "vsg-height" in html
    assert '<body class="">' in client.get("/").text


def test_root_path_prefixes_links(client, monkeypatch):
    monkeypatch.setattr(settings, "root_path", "/outils/voix-de-marque")
    html = client.get("/").text
    assert 'href="/outils/voix-de-marque/static/style.css?v=' in html
    assert 'const ROOT = "/outils/voix-de-marque";' in html
    r = client.get("/static/style.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    assert client.get("/static/..%2Fmain.py").status_code == 404
    assert client.get("/static/nope.css").status_code == 404
