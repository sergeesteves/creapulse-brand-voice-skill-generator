"""Porte-monnaie de crédits commun (creapulse-tools 079) : débit avant le modèle, remboursement si la
génération échoue, coût réel déclaré, et une file de renvoi quand WordPress ne répond pas.
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import gating, wallet
from app.config import settings
from app.distill import SECTIONS, DistillError, Guide, canonical_markdown
from app.scraper import PageText, word_count
from app.store import get_store
from app.wallet import WalletError
from app.wallet import call as real_call  # importé avant que le porte-monnaie factice ne le remplace
from conftest import login

PROSE = " ".join(["Une phrase d'article écrite pour de vrai, avec un vrai rythme."] * 60)
GUIDE = canonical_markdown({s: ("Une voix directe." if s == "Résumé en une phrase" else "- **trait** : « exemple »")
                            for s in SECTIONS})
URLS = {"urls": ["https://ex.fr/a"]}


@pytest.fixture
def api(monkeypatch):
    from app import main
    distilled = []

    async def fake_fetch(urls, client=None):
        return [PageText(u, "Titre", PROSE, word_count(PROSE), True, "", "fr") for u in urls]

    async def fake_distill(pages, name, client=None, meter=None):
        distilled.append(name)
        meter.add_llm_call({"prompt_tokens": 9000, "completion_tokens": 1500}, "0.0135")
        return Guide(GUIDE, {}, "Une voix directe.", "openai/gpt-5.4-mini", {})

    monkeypatch.setattr(main, "fetch_pages", fake_fetch)
    monkeypatch.setattr(main, "distill", fake_distill)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    monkeypatch.setattr(settings, "min_urls", 1)
    monkeypatch.setattr(settings, "account_daily_cap", 1000)
    with TestClient(main.app) as c:
        login(c, user_id=7)
        yield c, main, distilled


def test_the_real_cost_is_declared_after_a_generation(api, fake_wallet):
    client, _, _ = api
    assert client.post("/api/generate", json=URLS).status_code == 200
    [(_, debit)] = [c for c in fake_wallet.calls if c[0] == "debit"]
    [(_, cost)] = [c for c in fake_wallet.calls if c[0] == "cost"]
    assert cost == {"user_id": 7, "usage_ref": debit["usage_ref"], "cost_llm_usd": 0.0135, "cost_data_usd": 0.0,
                    "cost_other_usd": 0.0, "cost_complete": True}


def test_a_failed_distillation_is_refunded_and_its_cost_still_declared(api, monkeypatch, fake_wallet):
    client, main, _ = api

    async def failing(pages, name, client=None, meter=None):
        meter.add_llm_call({"prompt_tokens": 9000, "completion_tokens": 1500}, "0.0135")
        raise DistillError("profil non conforme")

    monkeypatch.setattr(main, "distill", failing)
    r = client.post("/api/generate", json=URLS)
    assert r.status_code == 503 and "remboursés" in r.json()["message"]
    assert fake_wallet.routes() == ["debit", "refund", "cost"]
    refs = {b["usage_ref"] for _, b in fake_wallet.calls}
    assert len(refs) == 1  # un seul usage, débité, remboursé, chiffré
    assert fake_wallet.calls[1][1]["reason"] == "distill_failed"
    assert fake_wallet.balance == 50


def test_a_debit_timeout_is_replayed_with_the_same_usage_ref(api, fake_wallet):
    client, _, distilled = api
    fake_wallet.fail("debit", WalletError(None, "ReadTimeout"))
    assert client.post("/api/generate", json=URLS).status_code == 200
    first, second = [b["usage_ref"] for route, b in fake_wallet.calls if route == "debit"]
    assert first == second and distilled and fake_wallet.balance == 40


def test_an_unreachable_wallet_launches_nothing_and_refunds_the_unknown_debit(api, fake_wallet):
    client, _, distilled = api
    fake_wallet.fail("debit", WalletError(503, "retry", {"retryable": True}), WalletError(None, "ConnectError"))
    r = client.post("/api/generate", json=URLS)
    assert r.status_code == 503 and r.json()["code"] == "wallet" and "Rien n'a été lancé" in r.json()["message"]
    assert distilled == [] and fake_wallet.routes() == ["debit", "debit", "refund"]
    assert fake_wallet.calls[2][1]["reason"] == "debit_timeout"
    assert get_store().get("global", "all") == 0


@pytest.mark.parametrize("status,data,code", [
    (403, {"closed_reason": "free_budget"}, "closed"),
    (403, {"closed_reason": "tool_disabled"}, "closed"),
    (404, {}, "unknown_member"),
    (410, {}, "unknown_member"),
    (401, {}, "wallet"),
])
def test_a_refused_debit_launches_nothing(api, fake_wallet, status, data, code):
    client, _, distilled = api
    fake_wallet.fail("debit", WalletError(status, "refused", data))
    r = client.post("/api/generate", json=URLS)
    assert r.json()["code"] == code and distilled == []
    assert "refund" not in fake_wallet.routes()  # refus net : rien à rembourser


def test_free_budget_message_says_paid_credits_still_work(api, fake_wallet):
    client, _, _ = api
    fake_wallet.fail("debit", WalletError(403, "closed", {"closed_reason": "free_budget"}))
    assert "Les crédits achetés restent utilisables" in client.post("/api/generate", json=URLS).json()["message"]


def test_a_lost_cost_is_queued_then_replayed_on_the_next_generation(api, fake_wallet):
    client, _, _ = api
    fake_wallet.fail("cost", WalletError(503, "retry"))
    client.post("/api/generate", json=URLS)
    [queued] = get_store().pending_wallet()
    assert queued["route"] == "cost" and json.loads(queued["body"])["user_id"] == 7

    client.post("/api/generate", json=URLS)
    assert get_store().pending_wallet() == []
    assert fake_wallet.routes().count("cost") == 3  # perdu, rejoué ; puis celui de la 2e génération


def test_a_terminal_refusal_leaves_the_queue(fake_wallet):
    store = get_store()
    store.enqueue_wallet("cost", json.dumps({"user_id": 7, "usage_ref": "voixabc:1"}), "ConnectError")
    store.enqueue_wallet("refund", json.dumps({"user_id": 8, "usage_ref": "voixdef:1"}), "ConnectError")
    fake_wallet.fail("cost", WalletError(410, "account_deleted"))
    fake_wallet.fail("refund", WalletError(None, "ConnectError"))
    assert asyncio.run(wallet.replay_outbox(None, store)) == 1
    [left] = store.pending_wallet()
    assert left["route"] == "refund" and left["attempts"] == 2


def test_the_http_call_follows_the_contract(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/debit"):
            return httpx.Response(402, json={"code": "credits_insufficient", "message": "…",
                                             "data": {"status": 402, "balance": 3, "required": 10,
                                                      "recharge_url": "https://staging.creapulse.fr/credits/"}})
        return httpx.Response(200, json={"balance": 3})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ok = await real_call(client, "balance", {"user_id": 7})
            with pytest.raises(WalletError) as err:
                await real_call(client, "debit", {"user_id": 7, "tier": "generation", "usage_ref": "voixabc:1"})
            return ok, err.value

    ok, err = asyncio.run(go())
    assert ok == {"balance": 3}
    assert err.status == 402 and err.data["required"] == 10 and not err.retryable
    req = seen[0]
    assert str(req.url) == "https://staging.creapulse.fr/wp-json/creapulse-tools/v1/credits/balance"
    assert req.headers["x-creapulse-tool"] == "voix" and req.headers["x-credits-secret"] == settings.credits_secret
    assert json.loads(req.content) == {"user_id": 7} and not req.url.query


def test_a_network_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("down")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await real_call(client, "balance", {"user_id": 7})

    with pytest.raises(WalletError) as err:
        asyncio.run(go())
    assert err.value.status is None and err.value.retryable
