"""Plafond de longueur du guide : garanti par le code, pas par la seule consigne au modèle.

Un profil trop long noie le modèle qui l'applique (mesuré le 2026-10-02 sur le guide Creapulse :
10 100 caractères, soit 70 % des consignes système du rédacteur).
"""
import asyncio

import pytest

from app import distill as distill_mod
from app.config import settings
from app.distill import (MIN_BULLETS, SECTIONS, TRIM_ORDER, build_system_prompt, canonical_markdown,
                         enforce_budget, parse_guide, split_bullets, trim_to_budget)

AXES = [s for s in SECTIONS if s != "Résumé en une phrase"]


def make_sections(n_bullets: int = 6, pad: int = 60) -> dict[str, str]:
    """Guide synthétique : n puces ancrées par axe, plus le résumé."""
    out = {}
    for axe in AXES:
        out[axe] = "\n".join(f"- **trait {axe[:4]} {i}** : « citation {i} »{'.' * pad}" for i in range(n_bullets))
    out["Résumé en une phrase"] = "Une voix orale et directe qui tranche."
    return out


def _run(coro):
    return asyncio.run(coro)


def test_split_bullets_keeps_multiline_bullets_whole():
    body = "- **premier** : « a »\n  suite du premier\n- **second** : « b »"
    bullets = split_bullets(body)
    assert len(bullets) == 2 and "suite du premier" in bullets[0]


def test_trim_cuts_the_least_voice_bearing_sections_first():
    sections = make_sections(n_bullets=6)
    full = len(canonical_markdown(sections))
    trimmed, removed = trim_to_budget(sections, full - 300)
    assert removed > 0 and len(canonical_markdown(trimmed)) <= full - 300
    # « Structure / format » encaisse les coupes avant tout le reste
    assert len(split_bullets(trimmed["Structure / format"])) < 6
    assert len(split_bullets(trimmed["Style de phrase"])) == 6
    assert trimmed["Résumé en une phrase"] == sections["Résumé en une phrase"]


def test_trim_never_empties_a_section_nor_the_summary():
    sections = make_sections(n_bullets=4)
    trimmed, _ = trim_to_budget(sections, 10)  # plafond absurde : impossible à tenir
    for axe in AXES:
        assert len(split_bullets(trimmed[axe])) >= MIN_BULLETS
    assert trimmed["Résumé en une phrase"] == sections["Résumé en une phrase"]
    # le guide reste relisible après coupe
    canonical, reparsed, summary = parse_guide(canonical_markdown(trimmed))
    assert set(reparsed) == set(SECTIONS) and summary == sections["Résumé en une phrase"]


def test_trim_order_covers_every_axis_and_spares_the_summary():
    assert set(TRIM_ORDER) == set(AXES)
    assert "Résumé en une phrase" not in TRIM_ORDER


def test_under_budget_costs_no_llm_call(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("aucun appel LLM ne doit partir sous le plafond")

    monkeypatch.setattr(distill_mod, "_chat", boom)
    monkeypatch.setattr(settings, "max_guide_chars", 50_000)
    sections = make_sections(n_bullets=3)
    canonical = canonical_markdown(sections)
    out, _, _, usage = _run(enforce_budget(canonical, sections, "Une voix.", None))
    assert out == canonical and usage == {}


def test_over_budget_triggers_one_shortening_pass_without_resending_articles(monkeypatch):
    calls = []
    short = make_sections(n_bullets=2, pad=10)

    async def fake_chat(messages, client, with_temperature=True):
        calls.append(messages)
        return canonical_markdown(short), {"total_tokens": 120}, "fake"

    monkeypatch.setattr(distill_mod, "_chat", fake_chat)
    sections = make_sections(n_bullets=8)
    canonical = canonical_markdown(sections)
    monkeypatch.setattr(settings, "max_guide_chars", len(canonical) - 500)

    out, out_sections, summary, usage = _run(enforce_budget(canonical, sections, "Une voix.", None))
    assert len(calls) == 1, "une seule passe de raccourcissement"
    sent = "".join(m["content"] for m in calls[0])
    assert "=== ARTICLE" not in sent, "les articles ne sont pas renvoyés (coût)"
    assert "GUIDE À RACCOURCIR" in sent and str(settings.max_guide_chars) in sent
    assert len(out) <= settings.max_guide_chars and usage["total_tokens"] == 120
    assert summary == "Une voix orale et directe qui tranche."


def test_code_enforces_the_cap_when_the_model_ignores_it(monkeypatch):
    """Le modèle renvoie un guide toujours trop long : le filet déterministe tranche quand même."""
    async def lazy_chat(messages, client, with_temperature=True):
        return canonical_markdown(make_sections(n_bullets=7)), {}, "fake"

    monkeypatch.setattr(distill_mod, "_chat", lazy_chat)
    sections = make_sections(n_bullets=8)
    canonical = canonical_markdown(sections)
    monkeypatch.setattr(settings, "max_guide_chars", 2000)
    out, out_sections, _, _ = _run(enforce_budget(canonical, sections, "Une voix.", None))
    assert len(out) <= 2000
    assert all(len(split_bullets(out_sections[a])) >= MIN_BULLETS for a in AXES)


def test_llm_failure_falls_back_to_the_deterministic_net(monkeypatch):
    async def broken(*a, **k):
        raise distill_mod.DistillError("LLM injoignable")

    monkeypatch.setattr(distill_mod, "_chat", broken)
    sections = make_sections(n_bullets=8)
    canonical = canonical_markdown(sections)
    monkeypatch.setattr(settings, "max_guide_chars", 2500)
    out, _, _, usage = _run(enforce_budget(canonical, sections, "Une voix.", None))
    assert len(out) <= 2500 and usage == {}


def test_system_prompt_carries_the_budget(monkeypatch):
    monkeypatch.setattr(settings, "max_guide_chars", 4200)
    prompt = build_system_prompt()
    assert "MOINS DE 4200 CARACTÈRES" in prompt and "{max_chars}" not in prompt
