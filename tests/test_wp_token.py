"""Jeton signé WordPress → outil (contrat creapulse-tools feature 016) + vecteur de référence pinné."""
import json

import pytest
from fastapi.testclient import TestClient

from app import gating, wp_token
from app.config import settings
from app.store import get_store

TEST_SECRET = "0123456789abcdef0123456789abcdef"
REF_PAYLOAD = {"sub": 42, "email": "membre@example.com", "name": "Membre Test", "levels": [1],
               "aud": "creapulse-tools", "iat": 1757376000, "exp": 1757376600}
REF_TOKEN = ("eyJzdWIiOjQyLCJlbWFpbCI6Im1lbWJyZUBleGFtcGxlLmNvbSIsIm5hbWUiOiJNZW1icmUgVGVzdCIsImxldmVscyI6WzFdLCJhdWQiOiJj"
             "cmVhcHVsc2UtdG9vbHMiLCJpYXQiOjE3NTczNzYwMDAsImV4cCI6MTc1NzM3NjYwMH0.vRV1jMbxlTuxKXoK89nq5BaWvAC4bYvc6SGr541Q_j0")
NOW = 1757376100  # dans la fenêtre de validité du vecteur


def test_reference_vector_matches_byte_for_byte():
    assert wp_token.make_token(REF_PAYLOAD, TEST_SECRET) == REF_TOKEN
    ident = wp_token.verify(REF_TOKEN, TEST_SECRET, now=NOW)
    assert ident and ident.user_id == 42 and ident.email == "membre@example.com" and ident.levels == (1,)
    assert ident.name == "Membre Test"


def test_verify_rejects_tampering_expiry_audience_and_garbage():
    assert wp_token.verify(REF_TOKEN, "autre-secret", now=NOW) is None
    assert wp_token.verify(REF_TOKEN, TEST_SECRET, now=REF_PAYLOAD["exp"]) is None          # expiré
    assert wp_token.verify(REF_TOKEN, TEST_SECRET, now=REF_PAYLOAD["iat"] - 120) is None    # iat trop dans le futur
    assert wp_token.verify(REF_TOKEN, TEST_SECRET, now=REF_PAYLOAD["iat"] - 30) is not None  # skew 60 s toléré
    payload_b64, sig = REF_TOKEN.split(".")
    assert wp_token.verify(payload_b64 + "x." + sig, TEST_SECRET, now=NOW) is None          # payload modifié
    assert wp_token.verify(payload_b64 + "." + sig[:-1] + "A", TEST_SECRET, now=NOW) is None  # signature modifiée
    for bad in ("", None, "abc", "a.b.c", "!!!.???", payload_b64 + "."):
        assert wp_token.verify(bad, TEST_SECRET, now=NOW) is None
    assert wp_token.verify(REF_TOKEN, "", now=NOW) is None  # secret absent → jamais accepté
    for mutation in ({"aud": "autre"}, {"sub": 0}, {"levels": "1"}, {"levels": [True]}, {"email": "pas-un-email"}):
        assert wp_token.verify(wp_token.make_token({**REF_PAYLOAD, **mutation}, TEST_SECRET), TEST_SECRET, now=NOW) is None


def test_unicode_and_slash_are_not_escaped():
    tok = wp_token.make_token({**REF_PAYLOAD, "name": "Éléonore / Test"}, TEST_SECRET)
    raw = wp_token._b64url_decode(tok.split(".")[0]).decode()
    assert '"name":"Éléonore / Test"' in raw and "\\u" not in raw and "\\/" not in raw
    assert json.loads(raw)["name"] == "Éléonore / Test"


@pytest.fixture
def client(monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "wp_token_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "min_urls", 1)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    monkeypatch.setattr(main.wp_token_mod, "time", type("T", (), {"time": staticmethod(lambda: NOW)}))
    with TestClient(main.app) as c:
        yield c


def test_index_consumes_token_sets_member_session_and_redirects(client):
    r = client.get(f"/?embed=1&wp_token={REF_TOKEN}&foo=bar", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/?embed=1&foo=bar"          # jeton (et login_url) retirés, le reste conservé
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    assert gating.SESSION_COOKIE in r.cookies
    sess = gating.read_session(r.cookies[gating.SESSION_COOKIE])
    assert sess.email == "membre@example.com" and sess.is_member and sess.levels == (1,)
    lead = get_store().get_lead("membre@example.com")
    assert lead["source"] == "wordpress" and lead["verified_at"] and lead["consent_at"]
    html = client.get("/?embed=1").text
    assert "Membre Creapulse : membre@example.com" in html and "Se déconnecter" not in html
    assert client.get("/").headers["referrer-policy"] == "no-referrer"


def test_bad_token_is_ignored_not_500(client):
    r = client.get(f"/?embed=1&wp_token={REF_TOKEN[:-3]}zzz", follow_redirects=False)
    assert r.status_code == 302 and gating.SESSION_COOKIE not in r.cookies
    assert r.headers["location"] == "/?embed=1"


def test_anonymous_gets_wordpress_links_when_provided(client):
    login = "https%3A%2F%2Fwww.creapulse.fr%2Flogin%2F%3Fredirect_to%3Dx"
    reg = "https%3A%2F%2Fwww.creapulse.fr%2Fmembership-checkout%2F%3Fpmpro_level%3D1"
    html = client.get(f"/?embed=1&login_url={login}&register_url={reg}").text
    assert 'href="https://www.creapulse.fr/login/?redirect_to=x" target="_top">Se connecter' in html
    assert 'href="https://www.creapulse.fr/membership-checkout/?pmpro_level=1" target="_top">Créer un compte' in html
    assert 'id="email-form" hidden' in html      # la capture email cède la place au compte Creapulse
    # URL hors site → ignorée
    html = client.get("/?embed=1&login_url=https%3A%2F%2Fevil.example%2Fphish").text
    assert "evil.example" not in html


def test_required_mode_blocks_anonymous_and_non_members(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "required")
    monkeypatch.setattr(settings, "required_levels", (2,))
    html = client.get("/").text
    assert "Réservé aux membres" in html and 'id="form-card" hidden' in html
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 403 and r.json()["code"] == "need_login"
    assert client.post("/api/magic-link", json={"email": "x@y.fr", "consent": True}).status_code == 403
    # membre niveau 1 seulement → toujours bloqué ; membre niveau 2 → ok
    client.get(f"/?wp_token={REF_TOKEN}", follow_redirects=False)
    assert "Réservé aux membres" in client.get("/").text
    monkeypatch.setattr(settings, "required_levels", (1, 2))
    assert "Réservé aux membres" not in client.get("/").text


def test_member_session_expires_after_member_ttl(client, monkeypatch):
    client.get(f"/?wp_token={REF_TOKEN}", follow_redirects=False)
    assert client.get("/").text.count("Membre Creapulse") == 1
    monkeypatch.setattr(settings, "member_session_ttl_s", -1)
    assert "Membre Creapulse" not in client.get("/").text
