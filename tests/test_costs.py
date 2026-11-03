"""Coût réel de chaque génération, pour le porte-monnaie de crédits de creapulse-tools.

Le coût d'un appel est celui que le routeur annonce ; à défaut, il est calculé à partir des jetons.
Il est enregistré pour chaque génération qui a appelé le modèle, échecs compris.
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import gating
from app.config import settings
from app.costs import COST_HEADER, CostMeter, header_cost
from app.distill import SECTIONS, DistillError, Guide, canonical_markdown, distill
from app.scraper import PageText, word_count
from app.store import get_store
from conftest import login

PROSE = " ".join(["Une phrase d'article écrite pour de vrai, avec un vrai rythme."] * 60)
GUIDE = canonical_markdown({s: ("Une voix directe." if s == "Résumé en une phrase" else "- **trait** : « exemple »")
                            for s in SECTIONS})


def test_routeur_cost_is_used_when_announced():
    meter = CostMeter()
    meter.add_llm_call({"prompt_tokens": 9000, "completion_tokens": 1500}, "0.0123")
    assert meter.llm_usd == pytest.approx(0.0123)
    assert meter.llm_source == "measured" and meter.complete


def test_cost_is_estimated_from_tokens_without_header():
    meter = CostMeter()
    meter.add_llm_call({"prompt_tokens": 9000, "completion_tokens": 1500}, None)
    # 9 000 × 0,75 $/M + 1 500 × 4,50 $/M
    assert meter.llm_usd == pytest.approx(0.0135)
    assert meter.llm_source == "estimated" and meter.complete


def test_a_zero_cost_with_tokens_is_not_trusted():
    """Un modèle sans ligne de prix dans le routeur annonce 0 : vu sur OpenRouter avant le 2026-09-30."""
    assert header_cost("0.0000000000") is None and header_cost("abc") is None and header_cost(None) is None
    meter = CostMeter()
    meter.add_llm_call({"prompt_tokens": 1000, "completion_tokens": 0}, "0.0000000000")
    assert meter.llm_usd == pytest.approx(0.00075) and meter.llm_source == "estimated"


def test_unknown_cost_makes_the_record_incomplete():
    meter = CostMeter()
    meter.add_llm_call({}, None)
    assert meter.llm_source == "unknown" and not meter.complete
    assert meter.contract() == {"cost_llm_usd": 0.0, "cost_data_usd": 0.0, "cost_other_usd": 0.0,
                                "cost_complete": False}


def test_distill_counts_every_answered_call_including_the_shortening_pass(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        body = {"choices": [{"message": {"content": GUIDE}}], "model": "openai/gpt-5.4-mini",
                "usage": {"prompt_tokens": 9000, "completion_tokens": 1500}}
        return httpx.Response(200, json=body, headers={COST_HEADER: "0.0135"})

    monkeypatch.setattr(settings, "max_guide_chars", len(GUIDE) - 1)  # force la passe de raccourcissement
    pages = [PageText(f"https://ex.fr/{i}", "Titre", PROSE, word_count(PROSE), True, "", "fr") for i in range(3)]
    meter = CostMeter()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await distill(pages, "Exemple", client=client, meter=meter)

    asyncio.run(go())
    assert len(calls) == 2  # distillation + raccourcissement
    assert meter.llm_calls == 2 and meter.llm_usd == pytest.approx(0.027) and meter.llm_source == "measured"


def test_a_refused_call_costs_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider down")

    meter = CostMeter()
    pages = [PageText("https://ex.fr/1", "Titre", PROSE, word_count(PROSE), True, "", "fr")]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await distill(pages, "Exemple", client=client, meter=meter)

    with pytest.raises(DistillError):
        asyncio.run(go())
    assert meter.llm_calls == 0 and meter.llm_usd == 0.0


@pytest.fixture
def api(monkeypatch):
    from app import main

    async def fake_fetch(urls, client=None):
        return [PageText(u, "Titre", PROSE, word_count(PROSE), True, "", "fr") for u in urls]

    monkeypatch.setattr(main, "fetch_pages", fake_fetch)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    monkeypatch.setattr(settings, "min_urls", 1)
    with TestClient(main.app) as c:
        login(c)
        yield c, main


def test_each_generation_records_its_cost(api, monkeypatch):
    client, main = api

    async def fake_distill(pages, name, client=None, meter=None):
        meter.add_llm_call({"prompt_tokens": 9000, "completion_tokens": 1500}, "0.0135")
        return Guide(GUIDE, {}, "Une voix directe.", "openai/gpt-5.4-mini", {})

    monkeypatch.setattr(main, "distill", fake_distill)
    r = client.post("/api/generate", json={"urls": ["https://ex.fr/a"]})
    assert r.status_code == 200
    assert not any(k.startswith("cost") for k in r.json())  # le coût reste interne, le visiteur ne le voit pas
    [row] = get_store().recent_generation_costs()
    assert row["status"] == "ok" and row["pages"] == 1 and row["llm_calls"] == 1
    assert row["cost_llm_usd"] == pytest.approx(0.0135) and row["cost_data_usd"] == 0 and row["cost_other_usd"] == 0
    assert row["cost_complete"] == 1 and row["cost_llm_source"] == "measured"
    assert not {"email", "url", "urls", "ip", "text", "guide"} & set(row)  # ni adresse, ni URL, ni texte


def test_a_failed_distillation_still_records_what_it_cost(api, monkeypatch):
    client, main = api

    async def failing_distill(pages, name, client=None, meter=None):
        meter.add_llm_call({"prompt_tokens": 9000, "completion_tokens": 1500}, "0.0135")
        meter.add_llm_call({"prompt_tokens": 10500, "completion_tokens": 1500}, "0.0146")
        raise DistillError("profil non conforme après relance")

    monkeypatch.setattr(main, "distill", failing_distill)
    r = client.post("/api/generate", json={"urls": ["https://ex.fr/a"]})
    assert r.status_code == 503
    [row] = get_store().recent_generation_costs()
    assert row["status"] == "distill_failed" and row["llm_calls"] == 2
    assert row["cost_llm_usd"] == pytest.approx(0.0281)


def test_a_failure_before_any_model_call_records_nothing(api, monkeypatch):
    client, main = api

    async def broken_fetch(urls, client=None):
        from app.scraper import ScrapeError
        raise ScrapeError("crawl4ai injoignable")

    monkeypatch.setattr(main, "fetch_pages", broken_fetch)
    r = client.post("/api/generate", json={"urls": ["https://ex.fr/a"]})
    assert r.status_code == 503
    assert get_store().recent_generation_costs() == []
