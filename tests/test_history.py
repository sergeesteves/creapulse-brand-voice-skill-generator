"""Historique des profils d'un compte identifié : retrouver, supprimer, et une base qui ne grossit pas.

Opt-in : un profil n'est gardé que si la case « Garder ce profil » est cochée. Le visiteur anonyme ne
garde rien, case cochée ou non. La conservation suit le niveau PMPro, comme le clustering ;
au-delà, le contenu est effacé et la ligne reste pour dire « expiré ».
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import gating
from app.config import settings
from app.distill import SECTIONS, Guide, canonical_markdown
from app.scraper import PageText, word_count
from app.store import get_store

PROSE = " ".join(["Une phrase d'article écrite pour de vrai, avec un vrai rythme."] * 60)
GUIDE = canonical_markdown({s: ("Une voix directe." if s == "Résumé en une phrase" else "- **trait** : « exemple »")
                            for s in SECTIONS})
URLS = ["https://ex.fr/a", "https://ex.fr/b", "https://ex.fr/c"]
KEEP = {"urls": URLS, "save": True}  # case « Garder ce profil » cochée


@pytest.fixture
def api(monkeypatch):
    from app import main

    async def fake_fetch(urls, client=None):
        return [PageText(u, "Titre " + u[-1], PROSE, word_count(PROSE), True, "", "fr") for u in urls]

    async def fake_distill(pages, name, client=None, meter=None):
        return Guide(GUIDE, {}, "Une voix directe.", "openai/gpt-5.4-mini", {})

    monkeypatch.setattr(main, "fetch_pages", fake_fetch)
    monkeypatch.setattr(main, "distill", fake_distill)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    monkeypatch.setattr(settings, "account_daily_cap", 1000)
    monkeypatch.setattr(settings, "global_daily_cap", 1000)
    with TestClient(main.app) as c:
        yield c


def login(client, email="a@b.fr", levels=()):
    client.cookies.set(gating.SESSION_COOKIE, gating.sign_session(email, "wordpress" if levels else "email", levels))


def test_retention_follows_the_most_generous_level(monkeypatch):
    monkeypatch.setattr(settings, "free_retention_months", 3)
    monkeypatch.setattr(settings, "level_retention_months", "5:6,7:12")
    assert settings.retention_months(()) == 3
    assert settings.retention_months((5,)) == 6
    assert settings.retention_months((5, 7)) == 12
    assert settings.retention_months((9,)) == 3
    monkeypatch.setattr(settings, "level_retention_months", "5:6,8:0")
    assert settings.retention_months((5, 8)) == 0  # 0 = sans limite


def test_an_identified_account_finds_its_profile_again(api):
    login(api)
    r = api.post("/api/generate", json=KEEP)
    assert r.status_code == 200
    generated = r.json()
    assert generated["id"]

    [item] = api.get("/api/history").json()["profiles"]
    assert item["id"] == generated["id"] and item["urls"] == URLS and not item["expired"]
    assert "guide" not in item  # la liste reste légère

    again = api.get(f"/api/history/{item['id']}").json()
    for key in ("name", "slug", "summary", "guide", "prompt_block", "skill_md", "pages", "suggestions"):
        assert again[key] == generated[key], key
    assert again["download_available"] is True


def test_without_the_box_nothing_is_kept(api):
    login(api)
    for payload in ({"urls": URLS}, {"urls": URLS, "save": False}, {"urls": URLS, "save": "true"}):
        r = api.post("/api/generate", json=payload)
        assert r.status_code == 200 and r.json()["id"] is None and r.json()["guide"] == GUIDE
    assert api.get("/api/history").json()["profiles"] == []
    with get_store()._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0


def test_an_anonymous_visitor_keeps_nothing(api):
    r = api.post("/api/generate", json=KEEP)
    assert r.status_code == 200 and r.json()["id"] is None
    assert api.get("/api/history").status_code == 403
    with get_store()._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0


def test_another_account_cannot_read_or_delete_a_profile(api):
    login(api, "a@b.fr")
    profile_id = api.post("/api/generate", json=KEEP).json()["id"]
    login(api, "c@d.fr")
    assert api.get("/api/history").json()["profiles"] == []
    assert api.get(f"/api/history/{profile_id}").status_code == 404
    assert api.delete(f"/api/history/{profile_id}").status_code == 404
    login(api, "a@b.fr")
    assert api.get(f"/api/history/{profile_id}").status_code == 200


def test_delete_removes_the_profile(api):
    login(api)
    profile_id = api.post("/api/generate", json=KEEP).json()["id"]
    assert api.delete(f"/api/history/{profile_id}").json() == {"ok": True}
    assert api.get("/api/history").json()["profiles"] == []
    assert api.get(f"/api/history/{profile_id}").status_code == 404


def test_the_account_keeps_only_its_most_recent_profiles(api, monkeypatch):
    monkeypatch.setattr(settings, "history_max_per_account", 3)
    login(api)
    ids = [api.post("/api/generate", json=KEEP).json()["id"] for _ in range(5)]
    listed = [p["id"] for p in api.get("/api/history").json()["profiles"]]
    assert listed == ids[:-4:-1]  # les 3 derniers, du plus récent au plus ancien


def test_an_expired_profile_loses_its_content_but_stays_listed(api):
    login(api)
    profile_id = api.post("/api/generate", json=KEEP).json()["id"]
    store = get_store()
    assert store.purge_expired_profiles(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=80)) == 0
    assert store.purge_expired_profiles(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=92)) == 1

    [item] = api.get("/api/history").json()["profiles"]
    assert item["expired"] and item["urls"] == URLS and item["summary"] is None
    r = api.get(f"/api/history/{profile_id}")
    assert r.status_code == 410 and r.json()["code"] == "expired" and r.json()["urls"] == URLS
    with store._conn() as c:
        assert c.execute("SELECT guide FROM profiles").fetchone()[0] is None


def test_unlimited_retention_is_never_purged(api, monkeypatch):
    monkeypatch.setattr(settings, "level_retention_months", "8:0")
    login(api, levels=(8,))
    api.post("/api/generate", json=KEEP)
    assert get_store().purge_expired_profiles(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3650)) == 0


def test_a_history_failure_never_costs_the_visitor_its_profile(api, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(get_store(), "save_profile", broken)
    login(api)
    r = api.post("/api/generate", json=KEEP)
    assert r.status_code == 200 and r.json()["id"] is None and r.json()["guide"] == GUIDE
